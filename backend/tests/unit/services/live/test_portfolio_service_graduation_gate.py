"""Stage-matrix regression for ``PortfolioService._is_graduated``.

Bug history (2026-05-13 paper-drill discovery): the gate queried
``GraduationCandidate.stage == "promoted"``, but the actual state
machine in ``services/graduation.py:VALID_TRANSITIONS`` has NO
``"promoted"`` stage — the chain ends at ``paused`` / ``live_running``.
Result: no strategy could EVER be added to a live portfolio.

This test enumerates every stage in ``GraduationService.ALL_STAGES`` and
asserts which ones are accepted vs rejected by ``_is_graduated``. The
completeness assertions (`test_stage_matrix_covers_every_known_stage` +
`test_eligible_constant_is_subset_of_known_stages`) ensure a future
stage addition cannot silently bypass classification — preventing the
same orphan-literal failure mode from re-occurring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, GraduationCandidate, Strategy
from msai.services.graduation import ELIGIBLE_FOR_LIVE_PORTFOLIO, GraduationService
from msai.services.live.portfolio_service import PortfolioService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


_MATRIX_PARAMS: list[tuple[str, bool]] = [
    ("discovery", False),
    ("validation", False),
    ("paper_candidate", False),
    ("paper_running", False),
    ("paper_review", False),
    ("live_candidate", True),
    ("live_running", True),
    ("paused", True),
    ("archived", False),
]


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(isolated_postgres_url: str) -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "expected_accepted"), _MATRIX_PARAMS)
async def test_graduation_gate_matrix(
    session_factory: async_sessionmaker,
    stage: str,
    expected_accepted: bool,
) -> None:
    """For every stage in the state machine, ``_is_graduated`` must return
    ``True`` iff that stage is in ``ELIGIBLE_FOR_LIVE_PORTFOLIO``."""
    async with session_factory() as session:
        strategy = Strategy(
            name=f"test-strategy-{stage}",
            file_path=f"/tmp/{stage}.py",
            strategy_class="TestStrategy",
            code_hash="a" * 64,
        )
        session.add(strategy)
        await session.flush()
        candidate = GraduationCandidate(
            strategy_id=strategy.id,
            stage=stage,
            config={},
            metrics={},
        )
        session.add(candidate)
        await session.commit()

        svc = PortfolioService(session)
        is_grad = await svc._is_graduated(strategy.id)
        assert is_grad is expected_accepted, (
            f"stage={stage!r}: gate returned {is_grad}, expected {expected_accepted}"
        )


def test_stage_matrix_covers_every_known_stage() -> None:
    """If someone adds a new stage to ``VALID_TRANSITIONS``, this test
    fails until they explicitly classify it as accepted or rejected in
    ``_MATRIX_PARAMS`` above.

    Prevents the orphan-literal failure mode (the bug this PR fixes):
    a code path referencing a stage that doesn't exist in the state
    machine. Forces synchronization between the gate definition and the
    state machine.
    """
    covered = {stage for stage, _ in _MATRIX_PARAMS}
    assert covered == GraduationService.ALL_STAGES, (
        f"matrix missing stages: {GraduationService.ALL_STAGES - covered}; "
        f"matrix has extra (not in VALID_TRANSITIONS): {covered - GraduationService.ALL_STAGES}"
    )


def test_eligible_constant_is_subset_of_known_stages() -> None:
    """``ELIGIBLE_FOR_LIVE_PORTFOLIO`` must be a subset of the state
    machine's stages — catches the orphan-literal failure mode at the
    constant-definition site itself."""
    assert ELIGIBLE_FOR_LIVE_PORTFOLIO <= GraduationService.ALL_STAGES, (
        f"ELIGIBLE_FOR_LIVE_PORTFOLIO contains stages not in VALID_TRANSITIONS: "
        f"{ELIGIBLE_FOR_LIVE_PORTFOLIO - GraduationService.ALL_STAGES}"
    )
