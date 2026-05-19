"""F1 integration tests -- mode branch on ``PortfolioService.run_portfolio_backtest``.

Verifies:

- Quick mode (the existing path) does NOT invoke the optimizer.
- Full mode delegates to ``portfolio_backtest.optimizer.run_portfolio_walk_forward``
  and persists ``is_metric`` / ``oos_metric`` / ``optimization_trace`` /
  ``walk_forward_payload`` to the :class:`PortfolioRun` row.

The Full-mode test stubs ``run_portfolio_walk_forward`` so the Optuna
loop + journal file backend are not exercised here -- that surface is
covered by the optimizer's own unit tests under
``tests/unit/services/portfolio_backtest/``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from msai.models.portfolio_enums import BacktestMode
from msai.models.portfolio_run import PortfolioRun
from msai.services.nautilus.backtest_runner import BacktestResult
from msai.services.portfolio import PortfolioService
from msai.services.portfolio_backtest.optimizer import PortfolioOptimizationResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from msai.models.portfolio import Portfolio


# ---------------------------------------------------------------------------
# Test doubles -- mirror the shape used by test_portfolio_job_orchestration.py
# ---------------------------------------------------------------------------


def _canned_account_df() -> pd.DataFrame:
    """Ten-bar fake account frame so the Quick path's returns extraction succeeds."""
    timestamps = pd.date_range("2024-01-02", periods=10, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "returns": [0.01, -0.005, 0.008, 0.003, -0.002, 0.004, 0.006, -0.001, 0.002, 0.005],
            "equity": [100_000.0 * (1.0 + 0.01 * (i + 1)) for i in range(10)],
        }
    )


class _StubRunner:
    """Stand-in for BacktestRunner.run -- skips subprocess + Nautilus."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> BacktestResult:
        self.calls.append(kwargs)
        return BacktestResult(
            orders_df=pd.DataFrame(),
            positions_df=pd.DataFrame(),
            account_df=_canned_account_df(),
            metrics={"total_return": 0.05, "sharpe": 1.2},
        )


class _StubReportGenerator:
    def __init__(self, tmp_path: Any) -> None:
        self.tmp_path = tmp_path

    def generate_tearsheet(
        self,
        returns: Any,
        benchmark: Any = None,
        title: str = "MSAI Backtest Report",
    ) -> str:
        return "<html><body>fake tearsheet</body></html>"

    def save_report(self, html: str, backtest_id: str, data_root: str) -> str:
        out = self.tmp_path / f"{backtest_id}.html"
        out.write_text(html)
        return str(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_run(
    session_factory: async_sessionmaker[AsyncSession],
    portfolio_id: UUID,
    mode: BacktestMode,
) -> PortfolioRun:
    """Create a PortfolioRun row via the lifecycle layer with the given mode."""
    async with session_factory() as session:
        # Lifecycle.create_run does not currently honour ``mode`` (B5 schema
        # exposes the field; the persistence wiring is Task F1's
        # responsibility).  Insert the row directly so the test can stamp
        # the mode column on the persisted row and exercise the new
        # mode-branching logic in run_portfolio_backtest.
        run = PortfolioRun(
            portfolio_id=portfolio_id,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 1),
            status="pending",
            mode=mode,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_mode_calls_existing_backtest_path(
    monkeypatch: pytest.MonkeyPatch,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
    tmp_path: Any,
) -> None:
    """Quick mode keeps the existing path; the optimizer is never invoked."""

    # Arrange
    portfolio = await make_portfolio_with_strategies(n=2)
    run = await _create_run(portfolio_session_factory, portfolio.id, BacktestMode.QUICK)

    # Spy on the optimizer entry point so we can assert it is NOT called.
    optimizer_calls: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> PortfolioOptimizationResult:
        optimizer_calls.append(kwargs)
        # The Quick path must never reach here; raise to make the failure
        # mode loud if the branching regresses.
        raise AssertionError("Quick mode must NOT call the optimizer")

    monkeypatch.setattr(
        "msai.services.portfolio_backtest.optimizer.run_portfolio_walk_forward",
        _spy,
    )

    # The Quick path touches ``ensure_catalog_data`` (catalog warmup) --
    # short-circuit it so the test stays isolated from Parquet IO.
    def _fake_ensure(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure,
    )

    # Act
    svc = PortfolioService()
    completed = await svc.run_portfolio_backtest(
        run.id,
        runner=_StubRunner(),
        report_generator=_StubReportGenerator(tmp_path),
        session_factory=portfolio_session_factory,
    )

    # Assert
    assert completed.status == "completed"
    assert optimizer_calls == [], "Quick mode must NOT call the optimizer"
    # Quick mode persists `metrics` + `series` + `allocations`, NOT IS/OOS.
    assert completed.metrics is not None
    assert completed.is_metric is None
    assert completed.oos_metric is None


@pytest.mark.asyncio
async def test_cancel_then_worker_completion_preserves_cancel_status(
    monkeypatch: pytest.MonkeyPatch,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
    tmp_path: Any,
) -> None:
    """Phase 5.1 P1-A — operator cancel must survive worker completion writes.

    Reproduces the race: operator flips the row to ``canceled`` after Phase 2
    finishes but before Phase 3's COMPLETED write lands. The orchestration
    must detect the terminal state and refuse to overwrite. Without the
    guard the worker's final commit would silently undo the cancel.
    """
    from msai.models.portfolio_enums import PortfolioRunStatus

    # Arrange — portfolio + run kicked off in QUICK mode.
    portfolio = await make_portfolio_with_strategies(n=2)
    run = await _create_run(portfolio_session_factory, portfolio.id, BacktestMode.QUICK)

    def _fake_ensure(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure,
    )

    # Inject a "cancel-after-backtest" hook on the report generator.
    # ``save_report`` is invoked in a thread executor by the orchestration
    # AFTER backtests finish but BEFORE the Phase 3 persist block opens
    # its session. The save_report callable runs OFF the event loop, so
    # we can ``run_coroutine_threadsafe`` an async session call onto the
    # running loop to flip the row to ``canceled``.
    import asyncio as _asyncio_top

    main_loop = _asyncio_top.get_running_loop()

    class _CancelingReportGen:
        def __init__(self, tmp: Any) -> None:
            self.tmp_path = tmp

        def generate_tearsheet(
            self,
            returns: Any,
            benchmark: Any = None,
            title: str = "MSAI Backtest Report",
        ) -> str:
            return "<html></html>"

        def save_report(self, html: str, backtest_id: str, data_root: str) -> str:
            async def _flip() -> None:
                async with portfolio_session_factory() as session:
                    row = await session.get(PortfolioRun, run.id)
                    assert row is not None
                    row.status = PortfolioRunStatus.CANCELED.value
                    await session.commit()

            # Schedule the cancel coroutine onto the test's running loop
            # and wait for it before returning so Phase 3 sees the flip.
            future = _asyncio_top.run_coroutine_threadsafe(_flip(), main_loop)
            future.result(timeout=5.0)

            out = Path(self.tmp_path) / f"{backtest_id}.html"
            out.write_text(html)
            return str(out)

    svc = PortfolioService()
    # Act — run the full Quick-mode path; the canceling report gen flips
    # the row to ``canceled`` BEFORE Phase 3's persist tries to write
    # COMPLETED.
    completed = await svc.run_portfolio_backtest(
        run.id,
        runner=_StubRunner(),
        report_generator=_CancelingReportGen(tmp_path),
        session_factory=portfolio_session_factory,
    )

    # Assert — the run row's final status is CANCELED, not COMPLETED.
    assert completed.status == PortfolioRunStatus.CANCELED.value, (
        f"cancel must win over worker completion; saw {completed.status}"
    )

    # Persistence check.
    async with portfolio_session_factory() as session:
        reloaded = await session.get(PortfolioRun, run.id)
        assert reloaded is not None
        assert reloaded.status == PortfolioRunStatus.CANCELED.value


@pytest.mark.asyncio
async def test_full_mode_cancel_during_optimizer_preserves_cancel_status(
    monkeypatch: pytest.MonkeyPatch,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
) -> None:
    """Phase 5.1 iter-2 P2 — Full-mode cancel must survive optimizer completion.

    Symmetric to ``test_cancel_then_worker_completion_preserves_cancel_status``
    for the Quick path.  The Full-mode persist block contains the same
    terminal-state guard (``orchestration.py:_run_full_mode`` ~L745-752);
    this test exercises it by stubbing the optimizer to flip the run row
    to CANCELED mid-optimization (simulating an operator cancel landing
    between the optimizer's last cancel_check poll and the worker's
    COMPLETED write).  The guard must detect the terminal state and
    refuse to overwrite — without it, the worker's final commit would
    silently undo the cancel.
    """
    from msai.models.portfolio_enums import PortfolioRunStatus
    from msai.services.portfolio_backtest.optimizer import PortfolioOptimizationResult

    # Arrange — portfolio + run kicked off in FULL mode.
    portfolio = await make_portfolio_with_strategies(n=2)
    run = await _create_run(portfolio_session_factory, portfolio.id, BacktestMode.FULL)

    # Stub catalog warmup (matches the other Full-mode tests).
    def _fake_ensure(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure,
    )

    # The optimizer runs in a worker thread via ``asyncio.to_thread``.
    # Schedule the cancel coroutine onto the main loop and wait so the
    # row is flipped BEFORE the Full-mode persist block opens its
    # session.  This mirrors the Quick-mode test's race shape exactly.
    import asyncio as _asyncio_top

    main_loop = _asyncio_top.get_running_loop()

    def _stub_walk_forward_then_cancel(**kwargs: Any) -> PortfolioOptimizationResult:
        async def _flip() -> None:
            async with portfolio_session_factory() as session:
                row = await session.get(PortfolioRun, run.id)
                assert row is not None
                row.status = PortfolioRunStatus.CANCELED.value
                await session.commit()

        # Flip the row to CANCELED on the main loop, then return a
        # "completed" optimization result.  The worker thread blocks
        # only for the few ms the DB write takes.
        future = _asyncio_top.run_coroutine_threadsafe(_flip(), main_loop)
        future.result(timeout=5.0)

        return PortfolioOptimizationResult(
            is_metric=1.0,
            oos_metric=1.0,
            generalization_gap=0.0,
            stability_ratio=1.0,
            best_config={"leverage": 1.0},
            optimization_trace=[],
            walk_forward_payload={"windows": []},
        )

    monkeypatch.setattr(
        "msai.services.portfolio_backtest.optimizer.run_portfolio_walk_forward",
        _stub_walk_forward_then_cancel,
    )

    # Act — pass the stub runner so the per-strategy cache build doesn't
    # spawn real Nautilus subprocesses.
    svc = PortfolioService()
    completed = await svc.run_portfolio_backtest(
        run.id,
        runner=_StubRunner(),
        session_factory=portfolio_session_factory,
    )

    # Assert — the run row's final status is CANCELED, not COMPLETED.
    assert completed.status == PortfolioRunStatus.CANCELED.value, (
        f"Full-mode cancel must win over optimizer completion; saw {completed.status}"
    )

    # Persistence check.
    async with portfolio_session_factory() as session:
        reloaded = await session.get(PortfolioRun, run.id)
        assert reloaded is not None
        assert reloaded.status == PortfolioRunStatus.CANCELED.value


@pytest.mark.asyncio
async def test_full_mode_uses_real_returns_not_synthetic(
    monkeypatch: pytest.MonkeyPatch,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
) -> None:
    """Phase 5.1 P0-A — Full mode must read REAL cached per-strategy returns.

    Asserts that ``_run_full_mode``'s trial body receives the cached
    real-backtest returns (built by running each member through the same
    Quick-mode Nautilus path) — NOT random noise like the pre-fix synthetic
    series. The stub runner returns a deterministic returns vector; the
    test inspects the optimizer's captured ``portfolio_backtest_fn`` to
    confirm a trial call against a small window produces metrics derived
    from the stub returns and not from the legacy noise generator.
    """
    from msai.services.portfolio_backtest.optimizer import PortfolioOptimizationResult

    portfolio = await make_portfolio_with_strategies(n=2)
    run = await _create_run(portfolio_session_factory, portfolio.id, BacktestMode.FULL)

    captured_kwargs: dict[str, Any] = {}

    def _stub_walk_forward(**kwargs: Any) -> PortfolioOptimizationResult:
        captured_kwargs.update(kwargs)
        return PortfolioOptimizationResult(
            is_metric=1.0,
            oos_metric=1.0,
            generalization_gap=0.0,
            stability_ratio=1.0,
            best_config={"leverage": 1.0},
            optimization_trace=[],
            walk_forward_payload={"windows": []},
        )

    monkeypatch.setattr(
        "msai.services.portfolio_backtest.optimizer.run_portfolio_walk_forward",
        _stub_walk_forward,
    )

    def _fake_ensure(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure,
    )

    # Deterministic stub returns — the trial body should compute its
    # metrics from THIS series, not from a random noise generator.
    stub_runner = _StubRunner()

    # Act
    svc = PortfolioService()
    await svc.run_portfolio_backtest(
        run.id,
        runner=stub_runner,
        session_factory=portfolio_session_factory,
    )

    # Assert — the optimizer was given a trial body bound to the real cache.
    assert "portfolio_backtest_fn" in captured_kwargs
    trial_fn = captured_kwargs["portfolio_backtest_fn"]

    # Invoke the trial body directly with the FULL window. Because the
    # stub runner returns a positive-mean returns series (mean ≈ +0.003),
    # the resulting total_return MUST be positive AND match the
    # compounded stub returns — not the previous noise generator's
    # mean-zero output.
    metrics = trial_fn(
        member_strategy_ids=captured_kwargs["member_strategy_ids"],
        allocator_name="equal_weight",
        risk_params={"leverage": 1.0, "position_size": 0.1},
        start_date=captured_kwargs["start_date"],
        end_date=captured_kwargs["end_date"],
        initial_capital=100_000.0,
    )
    assert isinstance(metrics, dict)
    assert "total_return" in metrics
    # Stub returns sum to a small positive value over 10 bars; with two
    # equal-weighted strategies and leverage=1.0 we should see a strictly
    # positive compounded return.  A noise-based generator with mean ≈ 0
    # could land negative; using REAL stub returns guarantees positive.
    assert metrics["total_return"] > 0.0, (
        f"Full-mode trial body must derive metrics from REAL cached returns; "
        f"got total_return={metrics['total_return']}"
    )
    # Ultrareview merged_bug_004 on PR #73: ``total_leverage`` now reports
    # REALIZED portfolio leverage (``leverage * sum(|w|)``) so
    # ``enforce_caps`` can catch derived-leverage violations from
    # combined per-strategy weights — non-normalized allocators
    # (``vol_targeted``) make this distinction load-bearing. With
    # leverage=1.0, position_size=0.1, and 2 equal-weight strategies
    # post-clip (each weight ≤ 0.1), the realized leverage is 1.0 * 0.2.
    assert metrics["total_leverage"] == pytest.approx(0.2)

    # Sanity check: the stub runner was actually called (the cache build
    # invoked it once per member).
    assert len(stub_runner.calls) >= 2, (
        f"cache build must run one backtest per member; saw {len(stub_runner.calls)}"
    )


@pytest.mark.asyncio
async def test_full_mode_calls_optimizer_and_persists_is_oos(
    monkeypatch: pytest.MonkeyPatch,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
    tmp_path: Any,
) -> None:
    """Full mode invokes the optimizer and persists IS/OOS + trace + payload.

    Phase 5.1 P0-A: Full mode now caches REAL per-strategy returns by
    running each member through the Quick-mode backtest path before the
    optimizer loop.  This test stubs both the catalog warmup and the
    candidate runner so the path stays isolated from Parquet IO.
    """

    # Arrange
    portfolio = await make_portfolio_with_strategies(n=2)
    run = await _create_run(portfolio_session_factory, portfolio.id, BacktestMode.FULL)

    fake_result = PortfolioOptimizationResult(
        is_metric=1.4,
        oos_metric=1.1,
        generalization_gap=0.3,
        stability_ratio=0.78,
        best_config={"leverage": 1.2},
        optimization_trace=[{"trial": 0, "is_score": 1.4, "oos_score": 1.1}],
        walk_forward_payload={"windows": [{"train_start": "2024-01-01"}]},
    )

    captured_kwargs: dict[str, Any] = {}

    def _stub_walk_forward(**kwargs: Any) -> PortfolioOptimizationResult:
        captured_kwargs.update(kwargs)
        return fake_result

    # _run_full_mode imports the symbol locally; patch at the source module.
    monkeypatch.setattr(
        "msai.services.portfolio_backtest.optimizer.run_portfolio_walk_forward",
        _stub_walk_forward,
    )

    # Stub the catalog warmup (matches the Quick-mode test). The Full-mode
    # path now runs ensure_catalog_data + a real per-strategy backtest each
    # to populate the returns cache, so both need to be short-circuited.
    def _fake_ensure(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure,
    )

    # Act — pass the stub runner so the per-strategy cache build doesn't
    # spawn real Nautilus subprocesses. _StubReportGenerator is unused for
    # Full mode (no QuantStats tearsheet on the Full path).
    svc = PortfolioService()
    completed = await svc.run_portfolio_backtest(
        run.id,
        runner=_StubRunner(),
        session_factory=portfolio_session_factory,
    )

    # Assert -- optimizer was invoked with the expected wiring.
    assert captured_kwargs["portfolio_id"] == str(portfolio.id)
    assert len(captured_kwargs["member_strategy_ids"]) == 2
    assert captured_kwargs["allocator_name"] == "equal_weight"
    # ``portfolio_backtest_fn`` must be a real closure (Task F2 wires the
    # real backtest call; for F1 we just check it is a callable).
    assert callable(captured_kwargs["portfolio_backtest_fn"])

    # Assert -- IS/OOS + trace + payload persisted to the run row.  The
    # ``is_metric`` / ``oos_metric`` columns are ``Numeric(18,6)``, so the
    # ORM returns ``Decimal`` — cast to ``float`` before approx-compare
    # (pytest.approx does not accept Decimal mixed-type operands).
    assert completed.status == "completed"
    assert completed.is_metric is not None
    assert completed.oos_metric is not None
    assert float(completed.is_metric) == pytest.approx(1.4)
    assert float(completed.oos_metric) == pytest.approx(1.1)
    assert completed.optimization_trace == [{"trial": 0, "is_score": 1.4, "oos_score": 1.1}]
    # ``walk_forward_payload`` was repurposed as the general "results payload"
    # for both Quick and Full modes (Task H+ backend enrichment).  Full mode
    # merges the optimizer's payload (``windows``) with per-strategy
    # enrichment keys (``per_strategy_equity`` / ``return_correlation`` etc.),
    # so we assert the optimizer keys are preserved AND the enrichment
    # landed, rather than exact equality.
    assert completed.walk_forward_payload is not None
    assert completed.walk_forward_payload.get("windows") == [{"train_start": "2024-01-01"}]
    assert "per_strategy_equity" in completed.walk_forward_payload
    assert "return_correlation" in completed.walk_forward_payload
    # ``metrics`` Full-mode shape: IS/OOS scalars + gap + stability + best_config
    assert completed.metrics is not None
    assert completed.metrics["is_metric"] == pytest.approx(1.4)
    assert completed.metrics["oos_metric"] == pytest.approx(1.1)
    assert completed.metrics["best_config"] == {"leverage": 1.2}

    # Persistence check -- reload from DB to confirm the commit landed.
    async with portfolio_session_factory() as session:
        reloaded = await session.get(PortfolioRun, run.id)
        assert reloaded is not None
        assert reloaded.is_metric is not None
        assert reloaded.oos_metric is not None
        assert float(reloaded.is_metric) == pytest.approx(1.4)
        assert float(reloaded.oos_metric) == pytest.approx(1.1)
        assert reloaded.status == "completed"


@pytest.mark.asyncio
async def test_create_run_inherits_default_mode_from_portfolio(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
) -> None:
    """Codex-bot PR-73 P2 regression -- when ``PortfolioRunCreate.mode`` is
    omitted (None), the lifecycle service inherits from ``Portfolio.default_mode``.

    Before the fix the schema defaulted to QUICK, so a portfolio created with
    ``default_mode='full'`` silently launched Quick runs whenever a client
    omitted the field.
    """
    from msai.schemas.portfolio import PortfolioRunCreate
    from msai.services.portfolio.lifecycle import PortfolioLifecycle

    # Arrange -- portfolio with default_mode=FULL (long enough range for Full)
    portfolio = await make_portfolio_with_strategies(n=1, default_mode=BacktestMode.FULL)

    # Act -- create run without specifying mode (should inherit FULL)
    async with portfolio_session_factory() as session:
        run = await PortfolioLifecycle.create_run(
            session,
            portfolio.id,
            PortfolioRunCreate(
                portfolio_id=portfolio.id,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
                # mode intentionally omitted
            ),
        )
        await session.commit()
        await session.refresh(run)

    # Assert -- run inherited the portfolio's default_mode
    assert run.mode == BacktestMode.FULL.value


@pytest.mark.asyncio
async def test_create_run_explicit_mode_overrides_default(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
) -> None:
    """Explicit ``mode`` in the request body wins over the portfolio's default."""
    from msai.schemas.portfolio import PortfolioRunCreate
    from msai.services.portfolio.lifecycle import PortfolioLifecycle

    portfolio = await make_portfolio_with_strategies(n=1, default_mode=BacktestMode.FULL)

    async with portfolio_session_factory() as session:
        run = await PortfolioLifecycle.create_run(
            session,
            portfolio.id,
            PortfolioRunCreate(
                portfolio_id=portfolio.id,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 31),
                mode=BacktestMode.QUICK,  # explicit override -- short range is fine for Quick
            ),
        )
        await session.commit()
        await session.refresh(run)

    assert run.mode == BacktestMode.QUICK.value


@pytest.mark.asyncio
async def test_create_run_inherited_full_mode_too_short_range_raises(
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
) -> None:
    """Inherited Full mode + too-short range raises at persist time.

    The schema validator only fires on explicit ``mode=FULL`` -- when mode is
    inherited (None in the request) the validator skips, so the lifecycle
    enforces the 90-day minimum directly.
    """
    from msai.schemas.portfolio import PortfolioRunCreate
    from msai.services.portfolio.lifecycle import PortfolioLifecycle

    portfolio = await make_portfolio_with_strategies(n=1, default_mode=BacktestMode.FULL)

    async with portfolio_session_factory() as session:
        with pytest.raises(ValueError, match="Full mode .* requires at least 90 days"):
            await PortfolioLifecycle.create_run(
                session,
                portfolio.id,
                PortfolioRunCreate(
                    portfolio_id=portfolio.id,
                    start_date=date(2024, 1, 1),
                    end_date=date(2024, 1, 30),  # 30-day range, too short
                    # mode omitted -- inherits FULL from portfolio
                ),
            )


@pytest.mark.asyncio
async def test_api_create_run_inherited_full_mode_too_short_range_returns_422(
    api_client_authed,
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
) -> None:
    """Codex bot iter-3 P2 on PR #73 — when the request body omits ``mode``
    and the portfolio's ``default_mode`` is FULL with a <90-day range, the
    API must return 422 with the actionable validation message — NOT 404.

    The pre-fix handler caught every ``ValueError`` from
    ``PortfolioLifecycle.create_run`` and converted it to
    ``404 "Portfolio {id} not found"``, which was both misleading (the
    portfolio exists) and unactionable (the user has no idea the 90-day
    rule is what's failing).
    """
    # Arrange — portfolio whose default_mode is FULL.
    portfolio = await make_portfolio_with_strategies(n=1, default_mode=BacktestMode.FULL)

    # Act — submit a run without ``mode`` (inherits FULL) over a 30-day
    # range, below the 90-day minimum.
    response = await api_client_authed.post(
        f"/api/v1/portfolios/{portfolio.id}/runs",
        json={
            "portfolio_id": str(portfolio.id),
            "start_date": "2024-01-01",
            "end_date": "2024-01-30",
            # mode intentionally omitted
        },
    )

    # Assert — 422 with the validation message; not a misleading 404.
    assert response.status_code == 422, response.text
    body = response.json()
    detail = body.get("detail", "")
    detail_str = detail if isinstance(detail, str) else str(detail)
    assert "Full mode" in detail_str
    assert "90 days" in detail_str


@pytest.mark.asyncio
async def test_quick_mode_honors_inverse_vol_allocator_choice(
    monkeypatch: pytest.MonkeyPatch,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
    tmp_path: Any,
) -> None:
    """Codex bot iter-4 P2 on PR #73 — Quick mode must honor
    ``portfolio.allocator_name`` (``inverse_vol`` / ``vol_targeted``).

    Previously, the objective-driven heuristic flow collapsed to
    equal-weight whenever candidate metrics were empty (the F1c-bridge
    case), so the operator's allocator selection was silently dropped.
    With the fix, the realized per-strategy returns drive the allocator
    after the per-strategy backtests complete and the saved member
    weights reflect the inverse-vol choice.
    """
    from msai.services.portfolio import PortfolioService

    # Arrange — portfolio with allocator_name=inverse_vol and 2 strategies.
    portfolio = await make_portfolio_with_strategies(n=2, allocator_name="inverse_vol")
    run = await _create_run(portfolio_session_factory, portfolio.id, BacktestMode.QUICK)

    # Replace _execute_candidate_backtests with a canned shim that emits
    # distinct return profiles per candidate: strategy 0 is low-vol
    # (constant +0.01), strategy 1 is high-vol (oscillating ±0.05). The
    # inverse_vol allocator should put MORE weight on the low-vol strategy
    # — that's the definitional behavior.
    timestamps_iso = [
        t.isoformat() for t in pd.date_range("2024-01-02", periods=10, freq="D", tz="UTC")
    ]
    # Strategy 0 = low vol (mostly stable +0.01); strategy 1 = high vol
    # (oscillating ±0.05). inverse_vol must weight strategy 0 higher.
    low_vol_returns = [0.011, 0.009, 0.010, 0.011, 0.009, 0.010, 0.011, 0.009, 0.010, 0.011]
    high_vol_returns = [0.05, -0.05, 0.05, -0.05, 0.05, -0.05, 0.05, -0.05, 0.05, -0.05]

    async def _fake_execute_candidate_backtests(
        self: Any,
        *,
        runner: Any,
        allocations: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        max_parallelism: int | None,
    ) -> list[dict[str, Any]]:
        results = []
        for i, allocation in enumerate(allocations):
            returns = low_vol_returns if i == 0 else high_vol_returns
            results.append(
                {
                    "candidate_id": allocation["candidate_id"],
                    "strategy_id": allocation["strategy_id"],
                    "strategy_name": allocation["strategy_name"],
                    "instruments": allocation["instruments"],
                    "weight": allocation["weight"],
                    "metrics": {"total_return": 0.05, "sharpe": 1.2},
                    "timestamps": timestamps_iso,
                    "returns": returns,
                    "config": allocation["config"],
                }
            )
        return results

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.PortfolioService._execute_candidate_backtests",
        _fake_execute_candidate_backtests,
    )

    def _fake_ensure(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure,
    )

    # Act
    svc = PortfolioService()
    completed = await svc.run_portfolio_backtest(
        run.id,
        runner=_StubRunner(),
        report_generator=_StubReportGenerator(tmp_path),
        session_factory=portfolio_session_factory,
    )

    # Assert — inverse_vol assigns MORE weight to the lower-vol strategy.
    # The objective-driven heuristic would have produced 50/50; the fix
    # makes the actual weights reflect realized vol.
    assert completed.status == "completed"
    assert completed.allocations is not None
    weights = [a["weight"] for a in completed.allocations]
    assert len(weights) == 2
    # Low-vol (strategy 0) has near-zero std → inverse_vol allocator
    # collapses to equal-weight only when std is exactly 0 for ALL
    # strategies (see ``InverseVolAllocator.compute``). Here strategy 1
    # has non-zero std, so strategy 0's huge 1/eps dominates. The
    # important assertion is "weights are not equal" — equal would mean
    # the allocator branch was skipped.
    assert weights[0] != pytest.approx(weights[1], rel=1e-3), (
        f"inverse_vol should produce non-equal weights for differently-vol "
        f"strategies; got {weights}"
    )
    # And the low-vol strategy should get the larger weight.
    assert weights[0] > weights[1], (
        f"inverse_vol should weight the low-vol strategy higher; got {weights}"
    )


@pytest.mark.asyncio
async def test_api_create_full_run_with_non_optimizer_objective_returns_422(
    api_client_authed,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    _seed_user,
) -> None:
    """Codex bot iter-6 P2 on PR #73 — Full mode with an objective that has
    no scorer in the OBJECTIVES registry (``equal_weight`` / ``manual``)
    must be rejected with 422 at run-creation time.

    Without the gate, every Optuna trial raises ValueError (objective_score
    rejects unregistered objectives), the optimizer catches and marks the
    trial FAIL, and the run finishes "completed" with all-zero IS/OOS
    scores — silently useless.
    """
    from uuid import uuid4

    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.models.strategy import Strategy

    user_id = _seed_user.id

    # Arrange — portfolio whose objective is equal_weight (no scorer)
    # AND default_mode=full. Inline construction so we can override the
    # fixture's hardcoded ``objective="maximize_sharpe"``.
    async with portfolio_session_factory() as session:
        strategy = Strategy(
            id=uuid4(),
            name=f"obj-{uuid4().hex[:6]}",
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
            config={"instruments": ["AAPL"]},
            metrics={"sharpe": 1.0},
        )
        session.add(candidate)
        await session.flush()

        portfolio = Portfolio(
            id=uuid4(),
            name=f"obj-{uuid4().hex[:8]}",
            objective="equal_weight",  # No scorer in OBJECTIVES registry.
            default_mode=BacktestMode.FULL,
            base_capital=100_000.0,
            requested_leverage=1.0,
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
        await session.commit()
        portfolio_id = portfolio.id

    # Act — submit a Full-mode run over a 100-day range (satisfies the
    # 90-day rule so the only remaining gate is the objective check).
    response = await api_client_authed.post(
        f"/api/v1/portfolios/{portfolio_id}/runs",
        json={
            "portfolio_id": str(portfolio_id),
            "start_date": "2024-01-01",
            "end_date": "2024-04-30",
            "mode": "full",
        },
    )

    # Assert — 422 with a message naming the offending objective + the
    # valid scorer set.
    assert response.status_code == 422, response.text
    body = response.json()
    detail = body.get("detail", "")
    detail_str = detail if isinstance(detail, str) else str(detail)
    assert "Full mode" in detail_str
    assert "equal_weight" in detail_str


@pytest.mark.asyncio
async def test_api_create_full_run_with_legacy_max_sharpe_objective_succeeds(
    api_client_authed,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    _seed_user,
) -> None:
    """Codex bot iter-8 P2 on PR #73 — the Fix-8 objective gate must honor
    the legacy ``max_sharpe`` alias, which existing DB rows still store
    (the rest of the portfolio stack already translates it to
    ``maximize_sharpe`` via ``_coerce_objective``). Without the alias
    handling, the gate would 422 these portfolios even though they are
    valid Full-mode targets.
    """
    from uuid import uuid4

    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.models.strategy import Strategy

    user_id = _seed_user.id

    # Arrange — portfolio with the legacy "max_sharpe" objective string
    # (pre-rename DB rows). The model.objective column is a plain String,
    # so we can persist the raw alias.
    async with portfolio_session_factory() as session:
        strategy = Strategy(
            id=uuid4(),
            name=f"legacy-{uuid4().hex[:6]}",
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
            config={"instruments": ["AAPL"]},
            metrics={"sharpe": 1.0},
        )
        session.add(candidate)
        await session.flush()

        portfolio = Portfolio(
            id=uuid4(),
            name=f"legacy-{uuid4().hex[:8]}",
            objective="max_sharpe",  # Legacy alias for maximize_sharpe
            default_mode=BacktestMode.FULL,
            base_capital=100_000.0,
            requested_leverage=1.0,
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
        await session.commit()
        portfolio_id = portfolio.id

    # Act — submit a Full-mode run satisfying the 90-day rule.
    response = await api_client_authed.post(
        f"/api/v1/portfolios/{portfolio_id}/runs",
        json={
            "portfolio_id": str(portfolio_id),
            "start_date": "2024-01-01",
            "end_date": "2024-04-30",
            "mode": "full",
        },
    )

    # Assert — Fix 12 means the lifecycle alias path no longer rejects
    # ``max_sharpe`` as an unknown objective. The downstream enqueue may
    # 503 because Redis isn't running in this integration shape, but the
    # validation gate has passed. The pre-fix failure mode was a 422
    # whose message mentions either ``Unknown`` or the legacy value
    # itself — assert that those do NOT appear.
    if response.status_code == 422:
        body_text = response.text.lower()
        assert "unknown" not in body_text and "max_sharpe" not in body_text, (
            f"alias path produced an objective-unknown 422: {response.text}"
        )
    # Otherwise the request progressed past the lifecycle gate — that's
    # all this test cares about. 201 (success) or 503 (Redis missing) are
    # both acceptable here.
    assert response.status_code in (201, 503), response.text
