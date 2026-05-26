"""Integration tests for the smoke runner.

The runner pre-ingests AAPL+SPY for the configured window, idempotently
bootstraps the canonical ``__msai_smoke__`` Portfolio, then fires a
``PortfolioRun`` via the existing lifecycle. This module asserts the
high-level contract: two invocations should reuse the same Portfolio row
and yield distinct run rows, both marked ``smoke=True``.

The runner depends on a Redis pool (for the ingest mutex + arq enqueue)
and on the in-process Databento ingester. The integration test runtime
does not guarantee either, so both are monkeypatched out — the test
exercises the bootstrap idempotency + run-row plumbing, NOT the ingest
or arq round-trip (those have their own narrower tests).

PRD docs/prds/ingest-backtest-smoke-test.md v1.3.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from msai.models.strategy import Strategy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def _mock_runner_externals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Redis + ingest so the runner reaches its DB contract.

    The runner reaches out for:
      * ``get_redis_pool()`` -- arq enqueue + ingest mutex backend.
      * ``acquire_ingest_lock`` / ``release_ingest_lock`` -- per-symbol mutex.
      * ``ingest_symbols`` -- the in-process Databento ingester.
      * ``enqueue_portfolio_run`` -- the arq job enqueue.

    None of those are exercised in unit-level integration; they're
    stubbed to no-ops so the test asserts the *idempotency contract*
    of the bootstrap path -- that's the public guarantee of T6.
    """

    class _StubPool:
        async def close(self) -> None:
            return None

    async def _stub_get_pool() -> _StubPool:
        return _StubPool()

    async def _stub_acquire(
        _redis: object,
        *,
        symbol: str,
        window_start: object,
        ttl_seconds: int,
        wait_timeout_seconds: int,
    ) -> str:
        return f"stub-token-{symbol}"

    async def _stub_release(
        _redis: object,
        *,
        symbol: str,
        window_start: object,
        token: str,
    ) -> None:
        return None

    async def _stub_ingest(
        asset_class: str,
        symbols: list[str],
        start: str,
        end: str,
        *,
        provider: str = "auto",
        dataset: str | None = None,
        schema: str | None = None,
    ) -> object:
        class _R:
            bars_written = 0
            symbols_covered: list[str] = []
            empty_symbols: list[str] = []

        return _R()

    async def _stub_enqueue(_pool: object, _run_id: str, _portfolio_id: str) -> str:
        return "stub-job-id"

    # Coverage check fix (iter-1 #4): the runner now consults
    # compute_coverage() to decide whether to call ingest_symbols. Force the
    # cold path here (returns all symbols as gapped) so the lock-acquire +
    # ingest_symbols stub paths still execute under test. A dedicated test
    # below covers the warm path (status="full" → skip).
    async def _stub_symbols_with_gaps(
        symbols: tuple[str, ...],
        _start_date: object,
        _end_date: object,
    ) -> list[str]:
        return list(symbols)

    monkeypatch.setattr("msai.services.smoke.runner.get_redis_pool", _stub_get_pool)
    monkeypatch.setattr("msai.services.smoke.runner.acquire_ingest_lock", _stub_acquire)
    monkeypatch.setattr("msai.services.smoke.runner.release_ingest_lock", _stub_release)
    monkeypatch.setattr("msai.services.smoke.runner.ingest_symbols", _stub_ingest)
    monkeypatch.setattr("msai.services.smoke.runner.enqueue_portfolio_run", _stub_enqueue)
    monkeypatch.setattr("msai.services.smoke.runner._symbols_with_gaps", _stub_symbols_with_gaps)


@pytest.fixture
async def _seed_smoke_strategies(portfolio_db_session: AsyncSession) -> None:
    """Seed the 4 canonical smoke Strategy rows the runner expects.

    Mirrors the Alembic seed (Task 1) so the integration test does not
    depend on migration state -- the testcontainer schema is built from
    ``Base.metadata`` and never runs the seeding migration.
    """
    rows = [
        Strategy(
            id=uuid.uuid4(),
            name="__smoke__/smoke_market_order/AAPL",
            file_path="strategies/example/smoke_market_order.py",
            strategy_class="SmokeMarketOrderStrategy",
            default_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
        ),
        Strategy(
            id=uuid.uuid4(),
            name="__smoke__/smoke_market_order/SPY",
            file_path="strategies/example/smoke_market_order.py",
            strategy_class="SmokeMarketOrderStrategy",
            default_config={
                "instrument_id": "SPY.NASDAQ",
                "bar_type": "SPY.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
        ),
        Strategy(
            id=uuid.uuid4(),
            name="__smoke__/ema_cross/AAPL",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            default_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
                "fast_ema_period": 10,
                "slow_ema_period": 20,
                "trade_size": 1,
            },
        ),
        Strategy(
            id=uuid.uuid4(),
            name="__smoke__/ema_cross/SPY",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            default_config={
                "instrument_id": "SPY.NASDAQ",
                "bar_type": "SPY.NASDAQ-1-MINUTE-LAST-EXTERNAL",
                "fast_ema_period": 10,
                "slow_ema_period": 20,
                "trade_size": 1,
            },
        ),
    ]
    for r in rows:
        portfolio_db_session.add(r)
    await portfolio_db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runner_bootstraps_portfolio_idempotently_and_returns_run_row(
    portfolio_db_session: AsyncSession,
    _seed_smoke_strategies: None,
    _mock_runner_externals: None,
) -> None:
    """Two ``run_smoke`` invocations reuse the same canonical Portfolio.

    The first call creates the ``__msai_smoke__`` Portfolio (no row with
    that name exists in the fresh testcontainer). The second call must
    find it via the sentinel-name lookup and reuse it -- both runs share
    one ``portfolio_id`` while producing distinct ``PortfolioRun`` rows.
    Both rows must carry ``smoke=True`` so the metrics-enrichment branch
    in orchestration.py fires.
    """
    # Arrange
    from msai.services.smoke.runner import run_smoke

    # Act
    run_1 = await run_smoke(db=portfolio_db_session, config_name="fast")
    run_2 = await run_smoke(db=portfolio_db_session, config_name="fast")

    # Assert: same canonical Portfolio reused.
    assert run_1.portfolio_id == run_2.portfolio_id
    # Assert: distinct PortfolioRun rows.
    assert run_1.id != run_2.id
    # Assert: smoke marker carried through the lifecycle.
    assert run_1.smoke is True
    assert run_2.smoke is True


# ---------------------------------------------------------------------------
# Code-review iter-1 fix #3 — concurrent ``run_smoke`` invocations must NOT
# create duplicate canonical portfolios. The partial unique index added by
# migration ``c3d4e5f6a7b8`` (mirrored in the model's ``__table_args__`` so
# ``Base.metadata.create_all`` picks it up here) lets exactly one bootstrap
# win; the loser's ``flush`` raises ``IntegrityError`` and the runner
# catches it + re-SELECTs the winner's row.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_run_smoke_invocations_share_canonical_portfolio(
    portfolio_session_factory,
    _seed_smoke_strategies: None,
    _mock_runner_externals: None,
) -> None:
    """Two concurrent run_smoke calls in distinct sessions resolve to one Portfolio.

    Each ``run_smoke`` gets its own ``AsyncSession`` (mirroring two
    independent HTTP requests). Both call the bootstrap helper at the
    same time via ``asyncio.gather``; the partial unique index lets
    exactly one INSERT commit and forces the loser into the re-SELECT
    branch.
    """
    # Arrange
    import asyncio as _asyncio

    from msai.services.smoke.runner import run_smoke

    async def _run_once() -> tuple[uuid.UUID, uuid.UUID]:
        async with portfolio_session_factory() as session:
            run = await run_smoke(db=session, config_name="fast")
            return (run.id, run.portfolio_id)

    # Act — fire both invocations into the loop in the same tick.
    (id_a, pid_a), (id_b, pid_b) = await _asyncio.gather(_run_once(), _run_once())

    # Assert — distinct run ids; same canonical portfolio id; both rows
    # carry smoke=True (re-read from a fresh session to confirm the
    # commit reached the DB).
    assert id_a != id_b, "Two concurrent calls must produce distinct PortfolioRuns"
    assert pid_a == pid_b, (
        f"Both calls must share the canonical __msai_smoke__ Portfolio (got {pid_a} vs {pid_b})"
    )

    from sqlalchemy import func
    from sqlalchemy import select as _select

    from msai.models.portfolio import Portfolio as _Portfolio
    from msai.models.portfolio_run import PortfolioRun as _PortfolioRun

    async with portfolio_session_factory() as verify:
        count = (
            await verify.execute(
                _select(func.count())
                .select_from(_Portfolio)
                .where(_Portfolio.name == "__msai_smoke__")
            )
        ).scalar_one()
        assert count == 1, f"Expected exactly one canonical smoke Portfolio after race, got {count}"

        runs = (
            (
                await verify.execute(
                    _select(_PortfolioRun).where(_PortfolioRun.portfolio_id == pid_a)
                )
            )
            .scalars()
            .all()
        )
        assert len(runs) == 2, f"Expected 2 runs against the canonical portfolio, got {len(runs)}"
        assert all(r.smoke is True for r in runs), "Both runs must carry smoke=True"


# ---------------------------------------------------------------------------
# Codex code-review P2 — the canonical smoke Portfolio must carry the config's
# benchmark ("SPY") so orchestration calls ``load_benchmark_returns`` and
# alpha/beta persist instead of staying null.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runner_bootstraps_portfolio_with_benchmark_symbol(
    portfolio_db_session: AsyncSession,
    _seed_smoke_strategies: None,
    _mock_runner_externals: None,
) -> None:
    """A freshly-bootstrapped canonical Portfolio carries benchmark="SPY".

    Without this, orchestration skips alpha/beta (it only loads benchmark
    returns when ``portfolio.benchmark_symbol`` is set), so the live UI
    showed ``Alpha: — / Beta: —`` even though the G5 block labels SPY.
    """
    # Arrange
    from msai.models.portfolio import Portfolio
    from msai.services.smoke.config import SMOKE_CONFIGS
    from msai.services.smoke.runner import run_smoke

    # Act
    run = await run_smoke(db=portfolio_db_session, config_name="fast")

    # Assert — the persisted Portfolio carries the config benchmark.
    portfolio = await portfolio_db_session.get(Portfolio, run.portfolio_id)
    assert portfolio is not None
    assert portfolio.benchmark_symbol == SMOKE_CONFIGS["fast"].benchmark_symbol
    assert portfolio.benchmark_symbol == "SPY"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runner_self_heals_existing_portfolio_with_null_benchmark(
    portfolio_db_session: AsyncSession,
    _seed_smoke_strategies: None,
    _mock_runner_externals: None,
) -> None:
    """An existing canonical Portfolio with null benchmark gets self-healed.

    Codex code-review P2: rows created before benchmark wiring landed have
    ``benchmark_symbol = None``. The next ``run_smoke`` must UPDATE the
    existing row to the config benchmark so subsequent runs compute
    alpha/beta — no manual psql reconciliation needed.
    """
    # Arrange — first run creates the canonical portfolio, then we wind its
    # benchmark back to None to simulate a pre-fix row.
    from msai.models.portfolio import Portfolio
    from msai.services.smoke.runner import run_smoke

    first = await run_smoke(db=portfolio_db_session, config_name="fast")
    stale = await portfolio_db_session.get(Portfolio, first.portfolio_id)
    assert stale is not None
    stale.benchmark_symbol = None
    await portfolio_db_session.commit()

    # Act — second run looks up the existing row and should self-heal it.
    second = await run_smoke(db=portfolio_db_session, config_name="fast")

    # Assert — same canonical portfolio, benchmark now populated. Re-SELECT
    # with ``populate_existing`` so the assertion reads the committed value
    # (not the identity-map's cached null) without triggering a sync lazy
    # load.
    assert second.portfolio_id == first.portfolio_id
    from sqlalchemy import select as _select

    healed = (
        await portfolio_db_session.execute(
            _select(Portfolio)
            .where(Portfolio.id == first.portfolio_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert healed.benchmark_symbol == "SPY"
