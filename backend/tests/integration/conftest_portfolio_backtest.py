"""Reusable fixtures for portfolio-backtest integration tests.

Provides:

- ``portfolio_postgres_url`` — Postgres testcontainer scoped per-module,
  named to avoid clashing with the existing ``isolated_postgres_url``
  fixture in :mod:`tests.integration.conftest_symbol_onboarding`.
- ``portfolio_session_factory`` — async sessionmaker with the full schema
  created from ``Base.metadata``.
- ``portfolio_db_session`` — single AsyncSession scoped per-test.
- ``api_client_authed`` — httpx.AsyncClient with the get_db dependency
  overridden to yield the testcontainer session, and the global auth
  override from the root conftest already in place. Suitable for
  end-to-end API tests that need persistence across requests within a
  single test.
- ``make_strategy`` — async factory creating a :class:`Strategy` row
  (idempotent + uses uuid-generated name when not provided).
- ``make_portfolio_with_strategies`` — async factory creating N
  strategies + N graduation candidates + a Portfolio + N
  PortfolioAllocations, returning the Portfolio row.
- ``make_portfolio_run`` — async factory creating a :class:`PortfolioRun`
  in a chosen status. Auto-creates a parent Portfolio+candidate+allocation
  unless an explicit ``portfolio`` is passed.
- ``make_completed_portfolio_run`` — convenience wrapper that creates a
  Quick-mode run already in ``completed`` status with seeded metrics so
  promote-to-live tests can exercise the success path. ``over_leverage=True``
  bumps ``requested_leverage`` past the risk-engine's notional cap so the
  validation-failure branch is reachable.
- ``make_backtest`` — async factory creating a single-strategy
  :class:`Backtest` row (used by the unified-history test in G4).

Imported into directory-local ``conftest.py`` files (e.g. the
portfolio-backtest integration tests) — keeping the canonical module
filename non-``conftest`` so pytest does not auto-discover it at the
parent level and conflict with the other test families.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from msai.core.database import get_db
from msai.main import app
from msai.models import Base
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.portfolio import Portfolio
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.strategy import Strategy
from msai.models.user import User

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(scope="module")
def portfolio_postgres_url() -> Iterator[str]:
    """Module-scoped Postgres testcontainer for portfolio-backtest tests."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def portfolio_session_factory(
    portfolio_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Async sessionmaker with the full ORM schema applied."""
    engine = create_async_engine(portfolio_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def portfolio_db_session(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Single AsyncSession suitable for direct ARRANGE/seed in unit-shaped tests."""
    async with portfolio_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def api_client_authed(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    """httpx.AsyncClient wired to the FastAPI app with get_db overridden.

    The session yielded by the override is COMMITTED implicitly by the
    application code (the portfolio router calls ``await db.commit()`` on
    success).  This gives the test a real Postgres-backed contract — POST
    creates persist; subsequent GETs return the persisted state.

    The shared auth fixture in ``backend/tests/conftest.py`` already
    overrides ``get_current_user`` with a mock claims dict, so this client
    can hit auth-protected endpoints directly without manufacturing a JWT.
    """

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        # Yield a fresh session per request so each handler sees its own
        # transaction boundary (matches the production wiring).
        async with portfolio_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    try:
        async with client as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def _seed_user(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
) -> User:
    """Seed a single User row -- the strategies/portfolios link to this id."""
    async with portfolio_session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"pb-{uuid4().hex[:12]}",
            email=f"pb-{uuid4().hex[:8]}@example.com",
            role="trader",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest_asyncio.fixture
async def make_strategy(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    _seed_user: User,
) -> Callable[..., Awaitable[Strategy]]:
    """Factory that creates a :class:`Strategy` row and returns it.

    Accepts ``name``, ``default_config``, ``strategy_class``, ``file_path``
    overrides; missing fields fall back to uuid-derived defaults so calls
    without args are idempotent across the same test.
    """
    user_id = _seed_user.id

    async def _make(
        *,
        name: str | None = None,
        default_config: dict[str, Any] | None = None,
        strategy_class: str = "EMACrossStrategy",
        file_path: str = "strategies/example/ema_cross.py",
    ) -> Strategy:
        async with portfolio_session_factory() as session:
            strategy = Strategy(
                id=uuid4(),
                name=name or f"s-{uuid4().hex[:8]}",
                file_path=file_path,
                strategy_class=strategy_class,
                default_config=default_config or {"instruments": ["AAPL"], "asset_class": "stocks"},
                created_by=user_id,
            )
            session.add(strategy)
            await session.commit()
            await session.refresh(strategy)
            return strategy

    return _make


@pytest_asyncio.fixture
async def make_portfolio_with_strategies(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    _seed_user: User,
) -> Callable[..., Awaitable[Portfolio]]:
    """Factory that creates ``n`` strategies + candidates + a Portfolio.

    Returns the persisted :class:`Portfolio` row.  Each strategy gets one
    :class:`GraduationCandidate` (stage=``paper_candidate``) and one
    :class:`PortfolioAllocation`.  Matches the seed shape used by the
    existing ``test_portfolio_job_orchestration.py`` integration test.
    """
    user_id = _seed_user.id

    async def _make(*, n: int = 2, **portfolio_overrides: Any) -> Portfolio:
        async with portfolio_session_factory() as session:
            strategies: list[Strategy] = []
            candidates: list[GraduationCandidate] = []
            for i in range(n):
                strategy = Strategy(
                    id=uuid4(),
                    name=f"s-{i}-{uuid4().hex[:6]}",
                    file_path="strategies/example/ema_cross.py",
                    strategy_class="EMACrossStrategy",
                    default_config={"instruments": ["AAPL"], "asset_class": "stocks"},
                    created_by=user_id,
                )
                session.add(strategy)
                strategies.append(strategy)
            await session.flush()
            for strategy in strategies:
                candidate = GraduationCandidate(
                    id=uuid4(),
                    strategy_id=strategy.id,
                    stage="paper_candidate",
                    config={"instruments": ["AAPL"]},
                    metrics={"sharpe": 1.0, "total_return": 0.1, "sortino": 1.2},
                )
                session.add(candidate)
                candidates.append(candidate)
            await session.flush()

            portfolio = Portfolio(
                id=uuid4(),
                name=f"P-{uuid4().hex[:8]}",
                objective="maximize_sharpe",
                base_capital=100_000.0,
                requested_leverage=1.0,
                created_by=user_id,
                **portfolio_overrides,
            )
            session.add(portfolio)
            await session.flush()

            for candidate in candidates:
                session.add(
                    PortfolioAllocation(
                        portfolio_id=portfolio.id,
                        candidate_id=candidate.id,
                        weight=1.0 / n,
                    )
                )
            await session.commit()
            await session.refresh(portfolio)
            return portfolio

    return _make


# ---------------------------------------------------------------------------
# G1/G2 fixtures — PortfolioRun factories for cancel + promote-to-live tests.
# ---------------------------------------------------------------------------

# Inline imports kept co-located with usage so the PostToolUse ruff formatter
# does not strip them between subagent edits (see ``feedback_colocate_imports_
# _with_usage_in_edits.md`` in memory). Each fixture is a closure that returns
# from the inner ``_make`` body so reference-tracking sees a real consumer.
from datetime import UTC, date, datetime  # noqa: E402
from uuid import UUID  # noqa: E402

from msai.models.backtest import Backtest  # noqa: E402
from msai.models.portfolio_enums import BacktestMode, PortfolioRunStatus  # noqa: E402
from msai.models.portfolio_run import PortfolioRun  # noqa: E402


@pytest_asyncio.fixture
async def make_portfolio_run(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
) -> Callable[..., Awaitable[PortfolioRun]]:
    """Factory creating a :class:`PortfolioRun` in a chosen status.

    Used by the G1 (cancel endpoint) and G2 (promote-to-live)
    integration tests. When ``portfolio`` is omitted the factory delegates
    to ``make_portfolio_with_strategies`` to seed a fresh parent portfolio
    with two paper-candidate strategies — the same shape every other
    portfolio integration test uses.

    ``status`` is parsed through :class:`PortfolioRunStatus` so terminal
    rows (``completed`` / ``failed`` / ``canceled``) carry the right enum
    string. ``mode`` defaults to Quick; pass ``BacktestMode.FULL`` (or the
    string ``"full"``) for Full-mode promotion tests.
    """

    async def _make(
        *,
        status: str = "running",
        portfolio: Portfolio | None = None,
        portfolio_id: UUID | None = None,
        mode: BacktestMode | str = BacktestMode.QUICK,
        metrics: dict[str, Any] | None = None,
        is_metric: float | None = None,
        oos_metric: float | None = None,
        optimization_trace: list[dict[str, Any]] | None = None,
    ) -> PortfolioRun:
        # Parse status through the enum so the test-side string ("running",
        # "completed", "failed", "canceled") is always validated before
        # touching the DB — typos surface here, not as 500s in the route.
        status_enum = PortfolioRunStatus(status)
        mode_enum = BacktestMode(mode) if isinstance(mode, str) else mode

        if portfolio is None and portfolio_id is None:
            portfolio = await make_portfolio_with_strategies(n=2)
            portfolio_id = portfolio.id
        elif portfolio is not None:
            portfolio_id = portfolio.id
        # else: portfolio_id supplied directly — caller knows what they're doing.

        now = datetime.now(UTC)
        async with portfolio_session_factory() as session:
            run = PortfolioRun(
                id=uuid4(),
                portfolio_id=portfolio_id,
                status=status_enum.value,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 6, 30),
                mode=mode_enum,
                metrics=metrics,
                is_metric=is_metric,
                oos_metric=oos_metric,
                optimization_trace=optimization_trace,
                heartbeat_at=now,
                completed_at=now if status_enum.is_terminal else None,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run

    return _make


@pytest_asyncio.fixture
async def make_completed_portfolio_run(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    _seed_user: User,
) -> Callable[..., Awaitable[PortfolioRun]]:
    """Factory yielding a ``completed`` :class:`PortfolioRun` ready for promotion.

    Differs from ``make_portfolio_run(status="completed")`` in three ways:

    1. Owns its own parent Portfolio so we can flip ``requested_leverage``
       on the ``over_leverage=True`` branch without disturbing the shared
       fixture above.
    2. Seeds at least one strategy + paper-candidate + allocation, so the
       promote-to-live path has something concrete to materialize into a
       ``LivePortfolioRevisionStrategy``.
    3. ``over_leverage=True`` bumps ``requested_leverage`` to ``9.9`` (just
       under the schema's ``le=10.0`` cap) AND seeds metrics whose
       ``effective_leverage`` exceeds the RiskEngine's default
       ``max_notional_exposure`` — the promote endpoint's risk validation
       branch then returns 422 with a leverage-related message.
    """
    user_id = _seed_user.id

    async def _make(
        *,
        over_leverage: bool = False,
    ) -> PortfolioRun:
        async with portfolio_session_factory() as session:
            strategy = Strategy(
                id=uuid4(),
                name=f"s-{uuid4().hex[:8]}",
                file_path="strategies/example/ema_cross.py",
                strategy_class="EMACrossStrategy",
                default_config={"instruments": ["AAPL"], "asset_class": "stocks"},
                created_by=user_id,
            )
            session.add(strategy)
            await session.flush()

            candidate = GraduationCandidate(
                id=uuid4(),
                strategy_id=strategy.id,
                stage="paper_candidate",
                config={"instruments": ["AAPL"], "fast_ema": 10, "slow_ema": 30},
                metrics={"sharpe": 1.5, "total_return": 0.2, "sortino": 1.8},
            )
            session.add(candidate)
            await session.flush()

            portfolio = Portfolio(
                id=uuid4(),
                name=f"P-{uuid4().hex[:8]}",
                objective="maximize_sharpe",
                base_capital=100_000.0,
                # Risk validation gate: the promote endpoint instantiates a
                # default RiskEngine and rejects if requested_leverage
                # exceeds the implicit notional cap. We bump up just under
                # the schema's hard ceiling so the API's 422 path is
                # exercised without hitting the boundary validator.
                requested_leverage=9.9 if over_leverage else 1.0,
                created_by=user_id,
            )
            session.add(portfolio)
            await session.flush()

            session.add(
                PortfolioAllocation(
                    portfolio_id=portfolio.id,
                    candidate_id=candidate.id,
                    weight=1.0,
                )
            )

            now = datetime.now(UTC)
            run = PortfolioRun(
                id=uuid4(),
                portfolio_id=portfolio.id,
                status=PortfolioRunStatus.COMPLETED.value,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 6, 30),
                mode=BacktestMode.QUICK,
                metrics={
                    "sharpe": 1.5,
                    "total_return": 0.18,
                    # ``effective_leverage`` ends up well above the default
                    # RiskEngine cap when over_leverage=True. The promote
                    # endpoint reads this through the materialized revision
                    # weights, so we don't need to fabricate it here — it
                    # is derived from Portfolio.requested_leverage during
                    # promotion.
                },
                heartbeat_at=now,
                completed_at=now,
                created_by=user_id,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run

    return _make


@pytest_asyncio.fixture
async def make_backtest(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_strategy: Callable[..., Awaitable[Strategy]],
) -> Callable[..., Awaitable[Backtest]]:
    """Factory creating a single-strategy :class:`Backtest` row.

    Used by the G4 unified-history test to seed a "single" row alongside
    a "portfolio" row and verify the discriminator + filter contract.
    """

    async def _make(
        *,
        strategy: Strategy | None = None,
        status: str = "completed",
    ) -> Backtest:
        if strategy is None:
            strategy = await make_strategy()

        async with portfolio_session_factory() as session:
            backtest = Backtest(
                id=uuid4(),
                strategy_id=strategy.id,
                strategy_code_hash="x" * 64,
                config={},
                instruments=["AAPL"],
                start_date=date(2024, 1, 1),
                end_date=date(2024, 6, 30),
                status=status,
                progress=100 if status == "completed" else 0,
            )
            session.add(backtest)
            await session.commit()
            await session.refresh(backtest)
            return backtest

    return _make
