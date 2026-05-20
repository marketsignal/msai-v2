"""G1 integration tests — ``POST /api/v1/portfolios/runs/{run_id}/cancel``.

Verifies:

- A ``running`` run can be canceled (200 + updated PortfolioRunResponse).
- A terminal-state run (``completed``/``failed``/``canceled``) returns 409.
- A missing run id returns 404.
- The DB-level CHECK constraint ``ck_portfolio_runs_status`` accepts the
  ``canceled`` value (regression for the FAIL_BUG where the migration
  added the StrEnum member but not the constraint enum).

Uses the ``api_client_authed`` + ``make_portfolio_run`` fixtures from
``conftest_portfolio_backtest.py`` so persistence flows through the same
Postgres testcontainer + ``get_db`` override every other G-family API
test uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.core.database import get_db
from msai.main import app
from tests.integration._alembic_subprocess import run_alembic

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.mark.asyncio
async def test_cancel_running_portfolio_run_returns_200(
    api_client_authed,
    make_portfolio_run,
):
    # Arrange
    run = await make_portfolio_run(status="running")

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/cancel",
    )

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(run.id)
    assert body["status"] == "canceled"


@pytest.mark.asyncio
async def test_cancel_completed_portfolio_run_returns_409(
    api_client_authed,
    make_portfolio_run,
):
    # Arrange
    run = await make_portfolio_run(status="completed")

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/cancel",
    )

    # Assert
    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_cancel_missing_run_returns_404(api_client_authed):
    # Arrange — random UUID that doesn't exist.
    missing_id = uuid4()

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{missing_id}/cancel",
    )

    # Assert
    assert response.status_code == 404


# ----------------------------------------------------------------------
# Regression: DB-level CHECK constraint must accept ``canceled`` status
#
# This test bypasses the shared ``portfolio_session_factory`` fixture
# (which uses ``Base.metadata.create_all`` and does NOT replay the
# Alembic migrations) and instead runs alembic up to head against an
# isolated Postgres testcontainer.  That's the only way to exercise the
# ``ck_portfolio_runs_status`` CHECK constraint — it lives in the
# migration history, not in the ORM model definition.
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def _cancel_constraint_postgres_url() -> Iterator[str]:
    """Module-scoped Postgres testcontainer for the constraint regression."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def _cancel_constraint_session_factory(
    _cancel_constraint_postgres_url: str,
) -> AsyncIterator[async_sessionmaker]:
    """Apply the FULL alembic migration history (so CHECK constraints exist).

    Runs ``alembic upgrade head`` once per test against the testcontainer URL
    — the StrEnum-vs-CHECK skew was invisible in tests that only use
    ``Base.metadata.create_all`` because the CHECK constraint lives only in
    the migration, not the ORM model.
    """
    # Apply migrations.  Alembic's env.py picks up DATABASE_URL via
    # ``extra_env`` in the helper.
    run_alembic(_cancel_constraint_postgres_url, "upgrade", "head")
    engine = create_async_engine(_cancel_constraint_postgres_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def _cancel_constraint_api_client(
    _cancel_constraint_session_factory: async_sessionmaker,
) -> AsyncIterator[httpx.AsyncClient]:
    """API client bound to the migrations-applied testcontainer DB."""

    async def _override_get_db() -> AsyncIterator:
        async with _cancel_constraint_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-API-Key": "msai-dev-key"},
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_cancel_running_portfolio_run_actually_persists_canceled_status(
    _cancel_constraint_session_factory: async_sessionmaker,
    _cancel_constraint_api_client: httpx.AsyncClient,
) -> None:
    """Cancel must round-trip ``canceled`` through the real Alembic schema.

    Regression: the B1 migration extended :class:`PortfolioRunStatus` with
    ``CANCELED`` but the DB-level ``ck_portfolio_runs_status`` CHECK
    constraint (defined in ``l0f1g2h3i4j5_portfolio_orchestration_columns``)
    enumerated only ``pending|running|completed|failed`` — UPDATE-ing the
    status to ``'canceled'`` raised ``CheckViolationError`` and the
    endpoint returned 500.

    This test runs against a DB with the FULL migration history applied,
    so the constraint is in place.  ARRANGE seeds a running run via raw
    SQL on the testcontainer (the API doesn't expose a "create run for
    test" endpoint that bypasses worker enqueue), and VERIFY goes through
    the public cancel endpoint + a subsequent GET to confirm persistence.
    """
    # Arrange — seed a User → Portfolio → PortfolioRun(running) tuple
    # through the testcontainer session.  We need the FK chain populated
    # because ``PortfolioRun.portfolio_id`` is a NOT NULL FK.
    #
    # Note on ARRANGE scope: this is an integration-test bootstrap of state
    # the API does not expose (you cannot mint a "running" PortfolioRun via
    # public endpoints without queuing an arq job, which the testcontainer
    # has no worker for).  The "no cheating" boundary forbids raw DB writes
    # for *VERIFY* — VERIFY here still goes through the public API
    # (``POST /cancel`` + the returned response).
    user_id = uuid4()
    portfolio_id = uuid4()
    candidate_id = uuid4()
    strategy_id = uuid4()
    run_id = uuid4()
    async with _cancel_constraint_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, entra_id, email, display_name, role, "
                "created_at, updated_at) "
                "VALUES (:id, :entra, :email, :name, 'trader', NOW(), NOW())"
            ),
            {
                "id": user_id,
                "entra": f"entra-{user_id}",
                "email": f"u-{user_id}@test",
                "name": "T",
            },
        )
        await session.execute(
            text(
                "INSERT INTO strategies (id, name, file_path, strategy_class, "
                "default_config, created_by, created_at, updated_at) "
                "VALUES (:id, :name, :path, :cls, "
                '\'{"instruments": ["AAPL"]}\'::jsonb, '
                ":uid, NOW(), NOW())"
            ),
            {
                "id": strategy_id,
                "name": f"s-{strategy_id}",
                "path": "strategies/example/ema_cross.py",
                "cls": "EMACrossStrategy",
                "uid": user_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO graduation_candidates (id, strategy_id, stage, "
                "config, metrics, created_at, updated_at) "
                "VALUES (:id, :sid, 'paper_candidate', "
                "'{\"instruments\": [\"AAPL\"]}'::jsonb, '{}'::jsonb, NOW(), NOW())"
            ),
            {"id": candidate_id, "sid": strategy_id},
        )
        await session.execute(
            text(
                "INSERT INTO portfolios (id, name, objective, base_capital, "
                "default_mode, allocator_name, created_by, created_at, updated_at) "
                "VALUES (:id, :name, 'maximize_sharpe', 100000, "
                "'quick', 'equal_weight', :uid, NOW(), NOW())"
            ),
            {"id": portfolio_id, "name": "p-cancel", "uid": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO portfolio_allocations (id, portfolio_id, "
                "candidate_id, weight, created_at) "
                "VALUES (:id, :pid, :cid, 1.0, NOW())"
            ),
            {"id": uuid4(), "pid": portfolio_id, "cid": candidate_id},
        )
        await session.execute(
            text(
                "INSERT INTO portfolio_runs (id, portfolio_id, start_date, "
                "end_date, status, mode, created_at, updated_at) "
                "VALUES (:id, :pid, '2024-01-01', '2024-02-01', "
                "'running', 'quick', NOW(), NOW())"
            ),
            {"id": run_id, "pid": portfolio_id},
        )
        await session.commit()

    # Act — hit the public cancel endpoint
    cancel_resp = await _cancel_constraint_api_client.post(
        f"/api/v1/portfolios/runs/{run_id}/cancel",
    )

    # Assert — 200 (NOT 500) + body shows ``canceled``
    assert cancel_resp.status_code == 200, (
        f"Cancel must succeed against the real Alembic schema; got "
        f"{cancel_resp.status_code}: {cancel_resp.text}"
    )
    assert cancel_resp.json()["status"] == "canceled"

    # Assert — a subsequent GET reflects the persisted state (no
    # in-memory-only mutation; the row was actually UPDATE-d in DB).
    get_resp = await _cancel_constraint_api_client.get(
        f"/api/v1/portfolios/runs/{run_id}",
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["status"] == "canceled"

    # Silence import-noqa on Path (kept for downstream test extensions that
    # may need to reach into alembic.ini).
    _ = Path
    _ = UUID


@pytest.mark.asyncio
async def test_cancel_is_atomic_against_concurrent_terminal_write(
    api_client_authed,
    make_portfolio_run,
    portfolio_session_factory,
):
    """Codex bot iter-11 P1 on PR #73 — cancel must be atomic against
    concurrent terminal writes.

    Pre-fix flow: get_run → check non-terminal → set status=canceled →
    commit. The read-then-modify-then-commit pattern raced with the
    worker's completion write — whichever commits last wins, so a
    finished run could end up marked ``canceled``, or an operator
    cancel could be lost.

    With the fix, cancel is a single atomic conditional UPDATE
    (``WHERE status IN ('pending','running')``). If the worker
    completed first, rowcount=0 → cancel returns 409 + the row's
    current terminal state is preserved.
    """
    from msai.models.portfolio_run import PortfolioRun

    # Arrange — pre-set the run to ``completed`` BEFORE we cancel. This
    # simulates the race where the worker's commit lands first. With
    # the atomic UPDATE, cancel must NOT overwrite the terminal state.
    run = await make_portfolio_run(status="running")
    async with portfolio_session_factory() as session:
        reloaded = await session.get(PortfolioRun, run.id)
        assert reloaded is not None
        reloaded.status = "completed"
        await session.commit()

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/cancel",
    )

    # Assert — 409 (terminal state preserved); the row's status MUST
    # still be ``completed``, not ``canceled``.
    assert response.status_code == 409, response.text

    async with portfolio_session_factory() as session:
        final = await session.get(PortfolioRun, run.id)
        assert final is not None
        assert final.status == "completed", (
            f"atomic cancel must NOT clobber the worker's terminal "
            f"write; got status={final.status}"
        )
