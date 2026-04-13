"""Integration tests for the research job lifecycle flow.

Exercises Strategy → ResearchJob → ResearchTrial creation and state
transitions against a real Postgres container (via testcontainers).
Also tests promotion from a completed research job to a graduation
candidate via GraduationService.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, Strategy, User
from msai.models.research_job import ResearchJob
from msai.models.research_trial import ResearchTrial
from msai.services.graduation import GraduationService

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
            entra_id=f"research-{uuid4().hex[:12]}",
            email=f"research-{uuid4().hex[:8]}@example.com",
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
    """Seed a Strategy row for research jobs to reference."""
    async with session_factory() as session:
        strategy = Strategy(
            id=uuid4(),
            name="test_ema_cross",
            description="Test strategy for research flow",
            strategy_class="EmaCrossStrategy",
            file_path="strategies/test/ema_cross.py",
            created_by=seed_user.id,
        )
        session.add(strategy)
        await session.commit()
        return strategy


# ---------------------------------------------------------------------------
# Tests: research job lifecycle
# ---------------------------------------------------------------------------


class TestResearchJobLifecycle:
    """Create a strategy -> create research job -> add trials -> verify state."""

    async def test_create_job_pending(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
    ) -> None:
        """A freshly created research job starts as 'pending' with progress 0."""
        async with session_factory() as session:
            job = ResearchJob(
                strategy_id=seed_strategy.id,
                job_type="parameter_sweep",
                config={"instruments": ["AAPL.SIM"], "objective": "sharpe"},
                status="pending",
                progress=0,
            )
            session.add(job)
            await session.commit()

            assert job.id is not None
            assert job.status == "pending"
            assert job.progress == 0

    async def test_full_lifecycle_pending_to_completed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
    ) -> None:
        """Walk a research job through pending -> running -> completed with trials."""
        async with session_factory() as session:
            # Arrange: create pending job
            job = ResearchJob(
                strategy_id=seed_strategy.id,
                job_type="parameter_sweep",
                config={"instruments": ["AAPL.SIM"], "objective": "sharpe"},
                status="pending",
                progress=0,
            )
            session.add(job)
            await session.flush()

            # Act: worker marks it running
            job.status = "running"
            job.progress = 50
            job.progress_message = "Evaluating trials"
            await session.flush()

            # Add trials
            for i in range(3):
                trial = ResearchTrial(
                    research_job_id=job.id,
                    trial_number=i,
                    config={"fast_period": 10 + i * 2},
                    metrics={"sharpe_ratio": 1.5 + i * 0.1, "total_return": 0.1 + i * 0.02},
                    status="completed",
                    objective_value=1.5 + i * 0.1,
                )
                session.add(trial)
            await session.flush()

            # Mark completed with best result
            job.status = "completed"
            job.progress = 100
            job.best_config = {"fast_period": 14}
            job.best_metrics = {"sharpe_ratio": 1.7, "total_return": 0.14}
            await session.commit()

            # Assert
            assert job.status == "completed"
            assert job.best_config is not None
            assert job.best_config["fast_period"] == 14
            assert job.best_metrics is not None
            assert job.best_metrics["sharpe_ratio"] == 1.7

    async def test_failed_job_records_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
    ) -> None:
        """A job that fails records the error message."""
        async with session_factory() as session:
            job = ResearchJob(
                strategy_id=seed_strategy.id,
                job_type="walk_forward",
                config={"instruments": ["MSFT.SIM"]},
                status="pending",
                progress=0,
            )
            session.add(job)
            await session.flush()

            # Simulate failure
            job.status = "failed"
            job.error_message = "Backtest engine timeout after 300s"
            await session.commit()

            assert job.status == "failed"
            assert "timeout" in job.error_message.lower()

    async def test_trial_numbers_are_unique_per_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
    ) -> None:
        """Duplicate trial_number within a job raises IntegrityError."""
        from sqlalchemy.exc import IntegrityError

        async with session_factory() as session:
            job = ResearchJob(
                strategy_id=seed_strategy.id,
                job_type="parameter_sweep",
                config={},
                status="running",
                progress=0,
            )
            session.add(job)
            await session.flush()

            trial_1 = ResearchTrial(
                research_job_id=job.id,
                trial_number=0,
                config={"fast_period": 10},
                status="completed",
            )
            session.add(trial_1)
            await session.flush()

            trial_dup = ResearchTrial(
                research_job_id=job.id,
                trial_number=0,  # duplicate
                config={"fast_period": 12},
                status="completed",
            )
            session.add(trial_dup)

            with pytest.raises(IntegrityError):
                await session.flush()


# ---------------------------------------------------------------------------
# Tests: research job to graduation promotion
# ---------------------------------------------------------------------------


class TestResearchToGraduationPromotion:
    """Create completed research job -> promote to graduation candidate."""

    async def test_promote_completed_job_to_graduation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
    ) -> None:
        """A completed research job can be promoted to a graduation candidate."""
        async with session_factory() as session:
            # Arrange: completed research job
            job = ResearchJob(
                strategy_id=seed_strategy.id,
                job_type="parameter_sweep",
                config={},
                status="completed",
                progress=100,
                best_config={"fast_period": 12},
                best_metrics={"sharpe_ratio": 2.1},
            )
            session.add(job)
            await session.flush()

            # Act: promote via graduation service
            grad_service = GraduationService()
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config=job.best_config,
                metrics=job.best_metrics,
                research_job_id=job.id,
                notes="Promoted from research sweep",
            )
            await session.commit()

            # Assert
            assert candidate.stage == "discovery"
            assert candidate.research_job_id == job.id
            assert candidate.config == {"fast_period": 12}
            assert candidate.metrics["sharpe_ratio"] == 2.1
            assert candidate.notes == "Promoted from research sweep"

    async def test_promote_without_research_job_is_allowed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
    ) -> None:
        """A graduation candidate can be created without a research job (manual entry)."""
        async with session_factory() as session:
            grad_service = GraduationService()
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={"period": 20},
                metrics={"sharpe_ratio": 1.2},
                notes="Manual entry — no research sweep",
            )
            await session.commit()

            assert candidate.stage == "discovery"
            assert candidate.research_job_id is None
