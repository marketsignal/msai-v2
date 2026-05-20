"""G2 integration tests — ``POST /api/v1/portfolios/runs/{id}/promote-to-live``.

Verifies:

- A ``completed`` Quick-mode run promotes successfully (201 + ids).
- A ``failed`` (non-``completed``) run is rejected with 422.
- A run that violates the RiskEngine's notional/leverage cap is rejected
  with 422 and the error message mentions "leverage".

The promote endpoint creates a LivePortfolio + frozen
LivePortfolioRevision carrying the same per-strategy composition the
backtest validated. The risk-engine gate is non-bypassable.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_promote_creates_live_portfolio_and_revision(
    api_client_authed,
    make_completed_portfolio_run,
):
    # Arrange
    run = await make_completed_portfolio_run()

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/promote-to-live",
        json={"account_id": "DUTEST123"},  # Paper-only enforcement
    )

    # Assert
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["live_portfolio_id"] is not None
    assert body["live_portfolio_revision_id"] is not None


@pytest.mark.asyncio
async def test_promote_failed_run_returns_422(
    api_client_authed,
    make_portfolio_run,
):
    # Arrange — failed runs cannot be promoted, even with valid risk config.
    run = await make_portfolio_run(status="failed")

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_promote_with_non_paper_account_returns_422(
    api_client_authed,
    make_completed_portfolio_run,
):
    """Phase 1 paper-only enforcement: live ``U...`` account ids are rejected.

    Reviewers (Phase 5.1) flagged that the paper-only guard had no test.
    POST with a live-style account id must 422 + ``code=PAPER_ONLY_ENFORCED``;
    no LivePortfolio row is created on this path.
    """
    # Arrange
    run = await make_completed_portfolio_run()

    # Act — live IB accounts start with ``U`` (not ``DU``); the guard
    # rejects everything that does not start with ``DU``.
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/promote-to-live",
        json={"account_id": "U1234567"},
    )

    # Assert
    assert response.status_code == 422, response.text
    body = response.json()
    detail = body.get("detail", body)
    code = detail.get("error", {}).get("code", "") if isinstance(detail, dict) else ""
    assert code == "PAPER_ONLY_ENFORCED", body


@pytest.mark.asyncio
async def test_promote_full_mode_filters_portfolio_level_optimizer_params(
    api_client_authed,
    make_portfolio_run,
    portfolio_session_factory,
):
    """Codex bot iter-3 P1 on PR #73 — Full-mode promotion must NOT merge
    portfolio-level optimizer search-space params (``leverage``,
    ``position_size``) into per-strategy member configs.

    These keys are PORTFOLIO-level risk controls — see
    ``services/portfolio_backtest/optimizer.py::build_search_space``. They
    are not part of any individual strategy's ``default_config``. If they
    leaked into ``LivePortfolioRevisionStrategy.config``, the downstream
    ``verify_member_matches_candidate`` would reject the first deploy with
    ``BINDING_MISMATCH`` (the synthesized ``live_candidate``'s config is
    seeded from the member, so the mismatch surfaces against any pre-
    existing graduation-pipeline candidate the operator might have).
    """
    from sqlalchemy import select

    from msai.models import LivePortfolioRevisionStrategy
    from msai.models.portfolio_enums import BacktestMode

    # Arrange — Full-mode run carrying the optimizer's winning portfolio-
    # level params. The fixture's strategies have
    # ``default_config = {"instruments": ["AAPL"], "asset_class": "stocks"}``
    # so neither key is a strategy param.
    best_config = {"leverage": 1.5, "position_size": 0.2}
    run = await make_portfolio_run(
        status="completed",
        mode=BacktestMode.FULL,
        metrics={
            "is_metric": 1.4,
            "oos_metric": 1.1,
            "best_config": best_config,
        },
    )

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert — 201 + the live revision's members do NOT carry the
    # portfolio-level keys.
    assert response.status_code == 201, response.text
    body = response.json()
    revision_id = body["live_portfolio_revision_id"]

    async with portfolio_session_factory() as session:
        members = (
            (
                await session.execute(
                    select(LivePortfolioRevisionStrategy).where(
                        LivePortfolioRevisionStrategy.revision_id == revision_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert members, "promote-to-live must materialize at least one member"
        for member in members:
            cfg = dict(member.config or {})
            assert "leverage" not in cfg, (
                f"portfolio-level ``leverage`` must not leak into member config: {cfg}"
            )
            assert "position_size" not in cfg, (
                f"portfolio-level ``position_size`` must not leak into member config: {cfg}"
            )


@pytest.mark.asyncio
async def test_promote_full_mode_flows_strategy_level_tunables_through(
    api_client_authed,
    portfolio_session_factory,
    _seed_user,
):
    """Companion to the filter test — strategy-level best_config keys that
    appear in the strategy's ``default_config`` MUST flow through to member
    configs. This proves Fix 2 is a filter, not a blanket drop.
    """
    from datetime import UTC, date, datetime
    from decimal import Decimal
    from uuid import uuid4

    from sqlalchemy import select

    from msai.models import LivePortfolioRevisionStrategy
    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.models.portfolio_enums import BacktestMode
    from msai.models.portfolio_run import PortfolioRun
    from msai.models.strategy import Strategy

    user_id = _seed_user.id

    # Arrange — strategy whose default_config includes a tunable param
    # (fast_period) that the optimizer would search over.
    async with portfolio_session_factory() as session:
        strategy = Strategy(
            id=uuid4(),
            name=f"tunable-{uuid4().hex[:6]}",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            default_config={
                "instruments": ["AAPL"],
                "asset_class": "stocks",
                "fast_period": 10,  # strategy-level tunable
            },
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
            name=f"tunable-{uuid4().hex[:8]}",
            objective="maximize_sharpe",
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
                weight=Decimal("1.0"),
            )
        )

        # best_config carries both a strategy-level tunable AND
        # portfolio-level params. Only the strategy-level one should land
        # in the member config.
        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status="completed",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            mode=BacktestMode.FULL,
            metrics={
                "is_metric": 1.4,
                "oos_metric": 1.1,
                "best_config": {
                    "fast_period": 25,  # strategy-level — should flow through
                    "leverage": 2.0,  # portfolio-level — should be filtered
                },
            },
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run_id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert
    assert response.status_code == 201, response.text
    body = response.json()
    revision_id = body["live_portfolio_revision_id"]

    async with portfolio_session_factory() as session:
        members = (
            (
                await session.execute(
                    select(LivePortfolioRevisionStrategy).where(
                        LivePortfolioRevisionStrategy.revision_id == revision_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 1
        cfg = dict(members[0].config or {})
        assert cfg.get("fast_period") == 25, (
            f"strategy-level tunable must flow through from best_config: {cfg}"
        )
        assert "leverage" not in cfg, f"portfolio-level params must still be filtered: {cfg}"


@pytest.mark.asyncio
async def test_promote_uses_run_allocations_weights_over_compose_time_weights(
    api_client_authed,
    portfolio_session_factory,
    _seed_user,
):
    """Codex bot iter-6 P2 on PR #73 — when ``run.allocations`` carries
    weights different from ``PortfolioAllocation.weight`` (e.g., a Quick
    run with an inverse_vol allocator reweighted post-backtest), promotion
    must use the RUN's weights so the live composition matches what the
    backtest actually validated.

    Without this fix, a portfolio backtested as 70/30 by inverse_vol on
    realized vols would deploy as 50/50 (the bridge's compose-time
    equal-weight fallback), silently diverging from the validated result.
    """
    from datetime import UTC, date, datetime
    from uuid import uuid4

    from sqlalchemy import select

    from msai.models import LivePortfolioRevisionStrategy
    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.models.portfolio_enums import BacktestMode
    from msai.models.portfolio_run import PortfolioRun
    from msai.models.strategy import Strategy

    user_id = _seed_user.id

    # Arrange — 2-strategy portfolio with bridge-style allocations
    # (weight=None at compose time), then a Quick run whose
    # ``allocations`` payload encodes 0.7 / 0.3 (simulating inverse_vol
    # output). Promotion must materialize those weights, not 0.5 / 0.5.
    async with portfolio_session_factory() as session:
        strategies = []
        candidates = []
        for i in range(2):
            strategy = Strategy(
                id=uuid4(),
                name=f"rw-{i}-{uuid4().hex[:6]}",
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
                metrics={"sharpe": 1.0},
            )
            session.add(candidate)
            candidates.append(candidate)
        await session.flush()

        portfolio = Portfolio(
            id=uuid4(),
            name=f"rw-{uuid4().hex[:8]}",
            objective="maximize_sharpe",
            allocator_name="inverse_vol",
            base_capital=100_000.0,
            requested_leverage=1.0,
            created_by=user_id,
        )
        session.add(portfolio)
        await session.flush()
        # Bridge case: compose-time weights are None.
        for candidate in candidates:
            session.add(
                PortfolioAllocation(
                    portfolio_id=portfolio.id,
                    candidate_id=candidate.id,
                    weight=None,
                )
            )

        # Run's allocations payload carries the post-allocator weights.
        run_allocations = [
            {
                "candidate_id": str(candidates[0].id),
                "strategy_id": str(strategies[0].id),
                "weight": 0.7,
                "timestamps": [],
                "returns": [],
            },
            {
                "candidate_id": str(candidates[1].id),
                "strategy_id": str(strategies[1].id),
                "weight": 0.3,
                "timestamps": [],
                "returns": [],
            },
        ]
        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status="completed",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            mode=BacktestMode.QUICK,
            metrics={"sharpe": 1.5},
            allocations=run_allocations,
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run_id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert — live revision uses the run's weights (0.7 / 0.3), not
    # the bridge's compose-time equal-weight fallback (0.5 / 0.5).
    assert response.status_code == 201, response.text
    body = response.json()
    revision_id = body["live_portfolio_revision_id"]

    async with portfolio_session_factory() as session:
        members = (
            (
                await session.execute(
                    select(LivePortfolioRevisionStrategy).where(
                        LivePortfolioRevisionStrategy.revision_id == revision_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 2
        weights_by_strategy = {str(m.strategy_id): float(m.weight) for m in members}
        assert weights_by_strategy[str(strategies[0].id)] == pytest.approx(0.7), (
            f"strategy 0 should get run's 0.7 weight; got {weights_by_strategy}"
        )
        assert weights_by_strategy[str(strategies[1].id)] == pytest.approx(0.3), (
            f"strategy 1 should get run's 0.3 weight; got {weights_by_strategy}"
        )


@pytest.mark.asyncio
async def test_promote_raises_when_unlinked_live_candidate_conflicts(
    api_client_authed,
    portfolio_session_factory,
    _seed_user,
):
    """Codex bot iter-5 P2 on PR #73 — when a strategy already has an
    UNLINKED ``live_candidate`` whose config does NOT match the member
    being promoted, ``materialize_from_backtest`` must surface the
    conflict at promotion time (422 + ``MATERIALIZATION_FAILED``) rather
    than silently creating a second ``live_candidate`` that would later
    fail with ``BINDING_AMBIGUOUS`` at the deploy step.
    """
    from datetime import UTC, date, datetime
    from decimal import Decimal
    from uuid import uuid4

    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.models.portfolio_enums import BacktestMode
    from msai.models.portfolio_run import PortfolioRun
    from msai.models.strategy import Strategy

    user_id = _seed_user.id

    # Arrange — strategy with both:
    #   1) a paper_candidate used by the portfolio (will become member).
    #   2) a PRE-EXISTING unlinked live_candidate with DIFFERENT config —
    #      simulating an operator who graduated the strategy manually
    #      with a different config before/alongside the portfolio flow.
    async with portfolio_session_factory() as session:
        strategy = Strategy(
            id=uuid4(),
            name=f"conflict-{uuid4().hex[:6]}",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            default_config={"instruments": ["AAPL"], "asset_class": "stocks"},
            created_by=user_id,
        )
        session.add(strategy)
        await session.flush()

        # The portfolio's allocation candidate (matches strategy default).
        portfolio_candidate = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy.id,
            stage="paper_candidate",
            config={"instruments": ["AAPL"]},
            metrics={"sharpe": 1.0},
        )
        # Operator's pre-existing unlinked live_candidate with a different
        # instrument set — this is the conflict trigger.
        operator_live_cand = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy.id,
            stage="live_candidate",
            config={"instruments": ["SPY"], "fast_period": 99},
            metrics={"sharpe": 1.5},
            deployment_id=None,
        )
        session.add_all([portfolio_candidate, operator_live_cand])
        await session.flush()

        portfolio = Portfolio(
            id=uuid4(),
            name=f"conflict-{uuid4().hex[:8]}",
            objective="maximize_sharpe",
            base_capital=100_000.0,
            requested_leverage=1.0,
            created_by=user_id,
        )
        session.add(portfolio)
        await session.flush()
        session.add(
            PortfolioAllocation(
                portfolio_id=portfolio.id,
                candidate_id=portfolio_candidate.id,
                weight=Decimal("1.0"),
            )
        )

        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status="completed",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            mode=BacktestMode.QUICK,
            metrics={"sharpe": 1.2},
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run_id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert — 422 + clear message about the ambiguous-candidate conflict.
    assert response.status_code == 422, response.text
    body = response.json()
    detail = body.get("detail", body)
    msg = detail.get("error", {}).get("message", "") if isinstance(detail, dict) else str(detail)
    assert "unlinked" in msg.lower() and "live_candidate" in msg.lower(), body
    assert "BINDING_AMBIGUOUS" in msg, body


@pytest.mark.asyncio
async def test_promote_creates_live_candidate_per_member_for_first_deploy(
    api_client_authed,
    make_completed_portfolio_run,
    portfolio_session_factory,
):
    """Codex bot iter-3 P1 on PR #73 — promote-to-live MUST materialize a
    deployable ``live_candidate`` GraduationCandidate for each member.

    Without it, the next ``POST /api/v1/live/start-portfolio`` would hit
    ``BINDING_NOT_GRADUATED`` because the start path's first-deploy
    lookup filters by ``stage == "live_candidate" AND deployment_id IS NULL``.
    The pre-fix promote path only created the revision; the bridge
    candidates sat at ``portfolio_default`` and were invisible to start.
    """
    from sqlalchemy import select

    from msai.models import LivePortfolioRevisionStrategy
    from msai.models.graduation_candidate import GraduationCandidate

    # Arrange
    run = await make_completed_portfolio_run()

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert — 201 plus a live_candidate per member ready for start-portfolio.
    assert response.status_code == 201, response.text
    body = response.json()
    revision_id = body["live_portfolio_revision_id"]

    async with portfolio_session_factory() as session:
        members = list(
            (
                await session.execute(
                    select(LivePortfolioRevisionStrategy).where(
                        LivePortfolioRevisionStrategy.revision_id == revision_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert members, "promotion must materialize at least one member"

        for member in members:
            unlinked_live = list(
                (
                    await session.execute(
                        select(GraduationCandidate).where(
                            GraduationCandidate.strategy_id == member.strategy_id,
                            GraduationCandidate.stage == "live_candidate",
                            GraduationCandidate.deployment_id.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(unlinked_live) == 1, (
                f"expected exactly one unlinked live_candidate for strategy "
                f"{member.strategy_id}; got {len(unlinked_live)}"
            )
            cand = unlinked_live[0]
            # Candidate config must include ``instruments`` so
            # ``candidate_instruments(candidate)`` succeeds at start time.
            assert "instruments" in (cand.config or {}), cand.config
            # And the candidate's instruments + non-instruments config must
            # match the frozen member exactly so
            # ``verify_member_matches_candidate`` will pass.
            assert set(cand.config.get("instruments", [])) == set(member.instruments), (
                f"candidate instruments {cand.config.get('instruments')} "
                f"must match member instruments {member.instruments}"
            )


@pytest.mark.asyncio
async def test_promote_with_over_leverage_fails_risk_validation(
    api_client_authed,
    make_completed_portfolio_run,
):
    """Risk-engine rejection MUST surface as 422 + leverage-tagged message.

    Bumps requested_leverage past the RiskEngine's notional cap on the
    promotion path so the validation branch fires.
    """
    # Arrange
    run = await make_completed_portfolio_run(over_leverage=True)

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert
    assert response.status_code == 422, response.text
    body = response.json()
    # Error envelope shape per ``.claude/rules/api-design.md`` — the route
    # raises ``HTTPException(detail={"error": {...}})`` so FastAPI wraps it
    # under ``detail``. Read either location defensively because the
    # global handler may unwrap on its way out.
    detail = body.get("detail", body)
    message = (
        detail.get("error", {}).get("message", "") if isinstance(detail, dict) else str(detail)
    ).lower()
    assert "leverage" in message, body


@pytest.mark.asyncio
async def test_promote_normalizes_explicit_weights_to_sum_to_one(
    api_client_authed,
    portfolio_session_factory,
    _seed_user,
):
    """Codex-bot PR-73 P2 regression -- explicit allocation weights must be
    normalized to sum to 1.0 in the live revision, matching the backtest
    path's ``normalize_weights`` behavior.

    Before the fix, two allocations of weight 0.8 each (sum=1.6) backtested
    as 50/50 (normalized) but promoted as 80%/80% (raw) -- the live
    composition diverged from what was validated.
    """
    from datetime import UTC, date, datetime
    from decimal import Decimal
    from uuid import uuid4

    from sqlalchemy import select

    from msai.models import LivePortfolioRevisionStrategy
    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.models.portfolio_enums import BacktestMode
    from msai.models.portfolio_run import PortfolioRun
    from msai.models.strategy import Strategy

    user_id = _seed_user.id

    # Arrange -- portfolio with 2 explicit allocations whose weights sum to 1.6
    async with portfolio_session_factory() as session:
        strategies = []
        candidates = []
        for i in range(2):
            strategy = Strategy(
                id=uuid4(),
                name=f"weight-norm-s{i}-{uuid4().hex[:6]}",
                file_path="strategies/example/ema_cross.py",
                strategy_class="EMACrossStrategy",
                default_config={"instruments": ["AAPL"]},
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
                metrics={"sharpe": 1.0},
            )
            session.add(candidate)
            candidates.append(candidate)
        await session.flush()

        portfolio = Portfolio(
            id=uuid4(),
            name=f"weight-norm-{uuid4().hex[:8]}",
            objective="maximize_sharpe",
            base_capital=100_000.0,
            requested_leverage=1.0,
            created_by=user_id,
        )
        session.add(portfolio)
        await session.flush()

        # The bug condition: two raw weights of 0.8 each, summing to 1.6.
        for candidate in candidates:
            session.add(
                PortfolioAllocation(
                    portfolio_id=portfolio.id,
                    candidate_id=candidate.id,
                    weight=Decimal("0.8"),
                )
            )

        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status="completed",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            mode=BacktestMode.QUICK,
            metrics={"sharpe": 1.5},
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run_id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert
    assert response.status_code == 201, response.text
    body = response.json()
    revision_id = body["live_portfolio_revision_id"]

    async with portfolio_session_factory() as session:
        members = (
            (
                await session.execute(
                    select(LivePortfolioRevisionStrategy).where(
                        LivePortfolioRevisionStrategy.revision_id == revision_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 2
        weights = sorted(float(m.weight) for m in members)
        # 0.8 / (0.8 + 0.8) == 0.5 each; sum == 1.0
        assert weights == pytest.approx([0.5, 0.5]), (
            f"explicit weights must be normalized to sum to 1.0; got {weights}"
        )
        assert sum(weights) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_progress_callback_jsonb_merge_preserves_best_config(
    portfolio_session_factory,
    _seed_user,
) -> None:
    """Codex bot iter-9 P2 on PR #73 — the progress writer's JSONB merge
    UPDATE must preserve any keys (e.g., ``best_config``) that the
    completion write landed between the progress coroutine's
    ``session.get`` and its UPDATE. Without the merge, the prior
    "set metrics = {...}" pattern overwrote them.

    Real-DB integration test: the semantic guarantee is at the SQL
    layer (PostgreSQL's ``metrics || {...}::jsonb`` operator).
    """
    from datetime import UTC, date, datetime
    from uuid import uuid4

    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_enums import BacktestMode, PortfolioRunStatus
    from msai.models.portfolio_run import PortfolioRun
    from msai.workers.portfolio_job import _portfolio_progress_callback

    # Arrange — seed a running portfolio run that already carries the
    # terminal-shape metrics dict, mimicking the race where Phase 3's
    # completion commit lands BEFORE the progress UPDATE runs.
    async with portfolio_session_factory() as session:
        portfolio = Portfolio(
            id=uuid4(),
            name=f"jsonb-merge-{uuid4().hex[:8]}",
            objective="maximize_sharpe",
            base_capital=100_000.0,
            requested_leverage=1.0,
            created_by=_seed_user.id,
        )
        session.add(portfolio)
        await session.flush()

        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status=PortfolioRunStatus.RUNNING.value,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            mode=BacktestMode.FULL,
            metrics={
                "best_config": {"leverage": 1.5},
                "is_metric": 1.2,
                "oos_metric": 1.1,
            },
            heartbeat_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    # Act — call the progress callback. It must MERGE its progress
    # keys onto the existing metrics dict, not replace it.
    async with portfolio_session_factory() as session:
        await _portfolio_progress_callback(session, run_id, 50, "halfway")

    # Assert — both the pre-existing keys AND the new progress keys are
    # present after the JSONB merge.
    async with portfolio_session_factory() as session:
        reloaded = await session.get(PortfolioRun, run_id)
        assert reloaded is not None
        merged = reloaded.metrics or {}
        # Load-bearing assertion: pre-existing keys preserved.
        assert merged.get("best_config") == {"leverage": 1.5}, merged
        assert merged.get("is_metric") == 1.2, merged
        assert merged.get("oos_metric") == 1.1, merged
        # New keys merged on top.
        assert merged.get("progress") == 50, merged
        assert merged.get("progress_message") == "halfway", merged


@pytest.mark.asyncio
async def test_concurrent_promotions_serialized_via_unique_index(
    api_client_authed,
    portfolio_session_factory,
    _seed_user,
) -> None:
    """Codex bot iter-10 P1 on PR #73 — the partial unique index
    ``uq_unlinked_live_candidate_per_strategy`` (migration
    ``72ea2fd4dda2``) plus the IntegrityError handler in
    ``materialize_from_backtest`` must keep promotion idempotent under
    contention.

    Simulates the race: pre-insert an unlinked live_candidate whose
    config MATCHES what promotion would synthesize, then promote. The
    promote path's select-then-insert sees the existing row first and
    reuses it; but even if the SELECT missed (stale cache) and the
    INSERT tried, the unique index would catch it and the handler
    would re-read the winner. Either way, exactly ONE unlinked
    live_candidate remains.
    """
    from datetime import UTC, date, datetime
    from uuid import uuid4

    from sqlalchemy import select

    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.models.portfolio_enums import BacktestMode
    from msai.models.portfolio_run import PortfolioRun
    from msai.models.strategy import Strategy

    user_id = _seed_user.id

    # Arrange — strategy + portfolio + completed Quick run, plus a
    # pre-existing unlinked live_candidate that matches the member
    # config that promotion would produce.
    async with portfolio_session_factory() as session:
        strategy = Strategy(
            id=uuid4(),
            name=f"concurrent-promo-{uuid4().hex[:6]}",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            default_config={"instruments": ["AAPL"], "asset_class": "stocks"},
            created_by=user_id,
        )
        session.add(strategy)
        await session.flush()

        paper_cand = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy.id,
            stage="paper_candidate",
            config={"instruments": ["AAPL"]},
            metrics={"sharpe": 1.0},
        )
        # Pre-existing concurrent-winner live_candidate with matching
        # config so the promotion path reuses it.
        existing_live = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy.id,
            stage="live_candidate",
            config={
                "instruments": ["AAPL"],
                "asset_class": "stocks",
            },
            metrics={},
            deployment_id=None,
        )
        session.add_all([paper_cand, existing_live])
        await session.flush()

        portfolio = Portfolio(
            id=uuid4(),
            name=f"concurrent-promo-{uuid4().hex[:8]}",
            objective="maximize_sharpe",
            base_capital=100_000.0,
            requested_leverage=1.0,
            created_by=user_id,
        )
        session.add(portfolio)
        await session.flush()
        session.add(
            PortfolioAllocation(
                portfolio_id=portfolio.id,
                candidate_id=paper_cand.id,
                weight=1.0,
            )
        )

        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status="completed",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            mode=BacktestMode.QUICK,
            metrics={"sharpe": 1.2},
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        run_id = run.id
        existing_live_id = existing_live.id

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run_id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert — 201, and only ONE unlinked live_candidate exists (the
    # original; the promotion reused it).
    assert response.status_code == 201, response.text

    async with portfolio_session_factory() as session:
        unlinked = list(
            (
                await session.execute(
                    select(GraduationCandidate).where(
                        GraduationCandidate.strategy_id == strategy.id,
                        GraduationCandidate.stage == "live_candidate",
                        GraduationCandidate.deployment_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(unlinked) == 1, (
            f"unique index must prevent duplicate unlinked live_candidates; "
            f"found {len(unlinked)}"
        )
        assert unlinked[0].id == existing_live_id, (
            f"promotion must reuse the matching pre-existing row; got {unlinked[0].id}"
        )


@pytest.mark.asyncio
async def test_promote_truncates_long_portfolio_name(
    api_client_authed,
    portfolio_session_factory,
    _seed_user,
) -> None:
    """Ultrareview bug_001 on PR #73 — a portfolio with a 128-char name
    (schema max) used to overflow the LivePortfolio.name String(128)
    column after appending the 15-char ``(run XXXXXXXX)`` suffix. Now
    the source name is truncated so the suffix always fits.
    """
    from datetime import UTC, date, datetime
    from uuid import uuid4

    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.portfolio import Portfolio
    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.models.portfolio_enums import BacktestMode
    from msai.models.portfolio_run import PortfolioRun
    from msai.models.strategy import Strategy

    user_id = _seed_user.id

    # Arrange — portfolio with the maximum-allowed name length (128).
    long_name = "L" + ("o" * 126) + "g"  # exactly 128 chars
    assert len(long_name) == 128

    async with portfolio_session_factory() as session:
        strategy = Strategy(
            id=uuid4(),
            name=f"long-name-{uuid4().hex[:6]}",
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
            name=long_name,
            objective="maximize_sharpe",
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

        run = PortfolioRun(
            id=uuid4(),
            portfolio_id=portfolio.id,
            status="completed",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            mode=BacktestMode.QUICK,
            metrics={"sharpe": 1.2},
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    # Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run_id}/promote-to-live",
        json={"account_id": "DUTEST123"},
    )

    # Assert — 201 (NOT a 422 MATERIALIZATION_FAILED from a column-overflow).
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_cancel_endpoint_stamps_completed_at(
    api_client_authed,
    make_portfolio_run,
    portfolio_session_factory,
) -> None:
    """Ultrareview bug_002 on PR #73 — cancel must stamp ``completed_at``
    in the same UPDATE as the status flip. Every other terminal
    transition (mark_run_failed, Phase 3 completion) does; before the
    fix the cancel endpoint left ``completed_at`` NULL, breaking the
    terminal-row invariant and dropping the UI's terminal timestamp.
    """
    from msai.models.portfolio_run import PortfolioRun

    run = await make_portfolio_run(status="running")

    response = await api_client_authed.post(
        f"/api/v1/portfolios/runs/{run.id}/cancel",
    )
    assert response.status_code == 200, response.text

    async with portfolio_session_factory() as session:
        canceled = await session.get(PortfolioRun, run.id)
        assert canceled is not None
        assert canceled.status == "canceled"
        assert canceled.completed_at is not None, (
            "cancel must stamp completed_at to preserve the terminal-row invariant"
        )
