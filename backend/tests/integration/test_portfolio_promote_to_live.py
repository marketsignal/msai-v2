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
