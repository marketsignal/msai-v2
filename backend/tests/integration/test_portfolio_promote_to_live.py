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
async def test_promote_full_mode_run_materializes_best_config(
    api_client_authed,
    make_portfolio_run,
    portfolio_session_factory,
):
    """Full-mode promotion merges ``best_config`` into the live revision.

    Reviewers (Phase 5.1) flagged that the optimizer's winning params were
    not being verified against the materialized live revision. This test
    creates a completed Full-mode run with a concrete ``best_config``, calls
    promote-to-live, and asserts the new
    :class:`LivePortfolioRevisionStrategy` carries those keys in its
    ``config`` dict.
    """
    from sqlalchemy import select

    from msai.models import LivePortfolioRevisionStrategy
    from msai.models.portfolio_enums import BacktestMode

    # Arrange — Full-mode run with a best_config that should flow into the
    # live revision's per-strategy config dict.
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

    # Assert — 201 + the live revision's members carry the best_config keys.
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
            assert cfg.get("leverage") == 1.5, cfg
            assert cfg.get("position_size") == 0.2, cfg


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
