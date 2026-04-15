"""Integration test for portfolio orchestration.

Exercises :meth:`PortfolioService.run_portfolio_backtest` end-to-end
against a real Postgres container with the full seed (User → Strategy →
GraduationCandidate → Portfolio → PortfolioAllocation → PortfolioRun)
and mocked Nautilus + ReportGenerator dependencies.  The goal is to
prove the DB → orchestration → DB round-trip works; the Nautilus engine
itself is tested separately in ``tests/unit/test_nautilus_backtest_runner.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import uuid4

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, Strategy, User
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.portfolio import Portfolio
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.portfolio_run import PortfolioRun
from msai.services.nautilus.backtest_runner import BacktestResult
from msai.services.portfolio_service import PortfolioService

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
async def seeded_portfolio(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    """Seed user + 2 strategies + 2 graduation candidates + portfolio + run.

    Returns a dict of the key IDs so the test can assert on them.
    """
    strategies_root = tmp_path_factory.mktemp("strategies")
    # Create minimal placeholder strategy files — BacktestRunner is mocked, so
    # these never actually get loaded; the path just has to be readable.
    for name in ("alpha", "beta"):
        (strategies_root / f"{name}.py").write_text("# placeholder\n")

    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"pf-{uuid4().hex[:12]}",
            email=f"pf-{uuid4().hex[:8]}@example.com",
            role="trader",
        )
        session.add(user)

        strategy_alpha = Strategy(
            id=uuid4(),
            name="alpha",
            description="Alpha test strategy",
            strategy_class="AlphaStrategy",
            file_path=str(strategies_root / "alpha.py"),
            default_config={"instruments": ["AAPL"], "asset_class": "stocks"},
            created_by=user.id,
        )
        strategy_beta = Strategy(
            id=uuid4(),
            name="beta",
            description="Beta test strategy",
            strategy_class="BetaStrategy",
            file_path=str(strategies_root / "beta.py"),
            default_config={"instruments": ["SPY"], "asset_class": "stocks"},
            created_by=user.id,
        )
        session.add_all([strategy_alpha, strategy_beta])
        await session.flush()

        candidate_alpha = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy_alpha.id,
            stage="promoted",
            config={"instruments": ["AAPL"]},
            metrics={"sharpe": 1.5, "total_return": 0.2, "sortino": 2.0},
        )
        candidate_beta = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy_beta.id,
            stage="promoted",
            config={"instruments": ["SPY"]},
            metrics={"sharpe": 0.8, "total_return": 0.1, "sortino": 1.1},
        )
        session.add_all([candidate_alpha, candidate_beta])
        await session.flush()

        portfolio = Portfolio(
            id=uuid4(),
            name="Test Portfolio",
            objective="maximize_sharpe",
            base_capital=100_000.0,
            requested_leverage=1.0,
            created_by=user.id,
        )
        session.add(portfolio)
        await session.flush()

        allocations = [
            PortfolioAllocation(
                portfolio_id=portfolio.id,
                candidate_id=candidate_alpha.id,
                weight=0.6,
            ),
            PortfolioAllocation(
                portfolio_id=portfolio.id,
                candidate_id=candidate_beta.id,
                weight=0.4,
            ),
        ]
        session.add_all(allocations)
        await session.flush()

        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status="pending",
            start_date=pd.Timestamp("2024-01-01").date(),
            end_date=pd.Timestamp("2024-01-31").date(),
            max_parallelism=1,
        )
        session.add(run)
        await session.commit()

        return {
            "user_id": user.id,
            "portfolio_id": portfolio.id,
            "run_id": run.id,
            "candidate_alpha_id": candidate_alpha.id,
            "candidate_beta_id": candidate_beta.id,
        }


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _canned_account_df() -> pd.DataFrame:
    """Ten-bar fake account frame with returns already populated."""
    timestamps = pd.date_range("2024-01-02", periods=10, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "returns": [0.01, -0.005, 0.008, 0.003, -0.002, 0.004, 0.006, -0.001, 0.002, 0.005],
            "equity": [100_000.0 * (1.0 + 0.01 * (i + 1)) for i in range(10)],
        }
    )


class _StubRunner:
    """Stand-in for BacktestRunner.run — skips subprocess + Nautilus."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return BacktestResult(
            orders_df=pd.DataFrame(),
            positions_df=pd.DataFrame(),
            account_df=_canned_account_df(),
            metrics={"total_return": 0.05, "sharpe": 1.2},
        )


class _StubReportGenerator:
    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path
        self.saved_reports: list[str] = []

    def generate_tearsheet(self, returns, benchmark=None, title="MSAI Backtest Report"):
        return "<html><body>fake tearsheet</body></html>"

    def save_report(self, html: str, backtest_id: str, data_root: str) -> str:
        out = self.tmp_path / f"{backtest_id}.html"
        out.write_text(html)
        self.saved_reports.append(str(out))
        return str(out)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_portfolio_backtest_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_portfolio: dict,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed 2 allocations, run orchestration, assert DB row shows completion."""

    # Patch ensure_catalog_data to return canonical IDs without touching disk.
    def _fake_ensure(
        *,
        symbols,
        raw_parquet_root,
        catalog_root,
        asset_class,
    ):
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio_service.ensure_catalog_data",
        _fake_ensure,
    )

    stub_runner = _StubRunner()
    stub_reports = _StubReportGenerator(tmp_path)
    stub_market = MagicMock()
    stub_market.get_bars.return_value = []  # no benchmark data

    service = PortfolioService()
    completed = await service.run_portfolio_backtest(
        seeded_portfolio["run_id"],
        runner=stub_runner,
        report_generator=stub_reports,
        market_data_query=stub_market,
        session_factory=session_factory,
    )

    # Runner was called once per allocation.
    assert len(stub_runner.calls) == 2
    symbols_used = {call["instrument_ids"][0] for call in stub_runner.calls}
    assert symbols_used == {"AAPL.NASDAQ", "SPY.NASDAQ"}

    # Returned row has the right status + populated fields.
    assert completed.status == "completed"
    assert completed.metrics is not None
    assert completed.metrics["num_strategies"] == 2
    assert "total_return" in completed.metrics
    assert "sharpe" in completed.metrics
    assert "effective_leverage" in completed.metrics
    assert completed.series is not None
    assert len(completed.series) > 0
    assert completed.allocations is not None
    assert len(completed.allocations) == 2
    weights = [float(a["weight"]) for a in completed.allocations]
    assert abs(sum(weights) - 1.0) < 1e-9  # normalized
    assert completed.report_path is not None
    assert completed.report_path.startswith(str(tmp_path))  # report actually written
    assert (tmp_path / f"{seeded_portfolio['run_id']}.html").exists()

    # Strategy config received by the runner should have instrument_id / bar_type
    # injected via _prepare_strategy_config (even though seed config only had
    # "instruments").
    for call in stub_runner.calls:
        assert "instrument_id" in call["strategy_config"]
        assert "bar_type" in call["strategy_config"]

    # Re-read from DB to prove persistence.
    async with session_factory() as session:
        reloaded = await session.get(PortfolioRun, seeded_portfolio["run_id"])
        assert reloaded is not None
        assert reloaded.status == "completed"
        assert reloaded.metrics is not None
        assert reloaded.metrics["num_strategies"] == 2
        assert reloaded.allocations is not None
        assert len(reloaded.allocations) == 2
        assert reloaded.completed_at is not None
        assert reloaded.heartbeat_at is not None
        assert reloaded.error_message is None


@pytest.mark.asyncio
async def test_run_portfolio_backtest_raises_when_run_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from msai.services.portfolio_service import PortfolioOrchestrationError

    service = PortfolioService()
    bogus_id = uuid4()
    with pytest.raises(PortfolioOrchestrationError, match="not found"):
        await service.run_portfolio_backtest(
            bogus_id,
            session_factory=session_factory,
        )


@pytest.mark.asyncio
async def test_run_portfolio_backtest_propagates_candidate_failure(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_portfolio: dict,
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backtest that raises mid-orchestration must surface, not be swallowed.

    The whole portfolio is considered invalid if one candidate cannot run
    — silently dropping it would mislead the UI about num_strategies.
    """

    def _fake_ensure(*, symbols, raw_parquet_root, catalog_root, asset_class):
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio_service.ensure_catalog_data",
        _fake_ensure,
    )

    class _BoomRunner:
        def run(self, **kwargs):
            raise RuntimeError("backtest subprocess crashed")

    service = PortfolioService()
    with pytest.raises(RuntimeError, match="backtest subprocess crashed"):
        await service.run_portfolio_backtest(
            seeded_portfolio["run_id"],
            runner=_BoomRunner(),
            session_factory=session_factory,
        )


@pytest.mark.asyncio
async def test_run_portfolio_backtest_raises_when_allocations_missing(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from msai.services.portfolio_service import PortfolioOrchestrationError

    # Seed a portfolio with zero allocations.
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"empty-{uuid4().hex[:12]}",
            email=f"empty-{uuid4().hex[:8]}@example.com",
            role="trader",
        )
        session.add(user)
        portfolio = Portfolio(
            id=uuid4(),
            name="Empty Portfolio",
            objective="equal_weight",
            base_capital=1000.0,
            requested_leverage=1.0,
            created_by=user.id,
        )
        session.add(portfolio)
        await session.flush()
        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status="pending",
            start_date=pd.Timestamp("2024-01-01").date(),
            end_date=pd.Timestamp("2024-01-31").date(),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    service = PortfolioService()
    with pytest.raises(PortfolioOrchestrationError, match="no allocations"):
        await service.run_portfolio_backtest(
            run_id,
            session_factory=session_factory,
        )
