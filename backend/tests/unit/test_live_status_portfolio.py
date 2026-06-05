"""Tests for portfolio deployment response schemas."""

from __future__ import annotations

from uuid import uuid4

from msai.schemas.live import (
    LiveDeploymentInfo,
    LiveDeploymentStatusResponse,
    PortfolioDeploymentInfo,
    StrategyMemberInfo,
)


def test_strategy_member_info_fields():
    m = StrategyMemberInfo(
        strategy_id=uuid4(),
        strategy_id_full="EMA-0-abc123",
        instruments=["AAPL.NASDAQ"],
        weight="0.5",
    )
    assert m.strategy_id_full == "EMA-0-abc123"
    assert m.weight == "0.5"


def test_portfolio_deployment_info_includes_members():
    info = PortfolioDeploymentInfo(
        id=uuid4(),
        portfolio_revision_id=uuid4(),
        account_id="DU123",
        status="running",
        paper_trading=True,
        deployment_slug="abc123def456",
        members=[
            StrategyMemberInfo(
                strategy_id=uuid4(),
                strategy_id_full="EMA-0-abc",
                instruments=["AAPL.NASDAQ"],
                weight="0.5",
            ),
            StrategyMemberInfo(
                strategy_id=uuid4(),
                strategy_id_full="RSI-1-abc",
                instruments=["MSFT.NASDAQ"],
                weight="0.5",
            ),
        ],
    )
    assert len(info.members) == 2


def test_portfolio_deployment_info_defaults_empty_members():
    info = PortfolioDeploymentInfo(
        id=uuid4(),
        account_id="DU123",
        status="stopped",
        paper_trading=True,
        deployment_slug="abc123",
    )
    assert info.members == []
    assert info.portfolio_revision_id is None


def test_live_deployment_info_carries_portfolio_revision_id():
    """The list-status item must expose ``portfolio_revision_id`` so an
    operator can rediscover the frozen revision id required to
    warm-restart-redeploy via ``POST /live/start-portfolio`` (found by
    verify-e2e 2026-06-05 — no other sanctioned read surfaced it)."""
    revision_id = uuid4()
    info = LiveDeploymentInfo(
        id=uuid4(),
        status="running",
        paper_trading=True,
        portfolio_revision_id=revision_id,
    )
    assert info.portfolio_revision_id == revision_id


def test_live_deployment_status_response_carries_portfolio_revision_id():
    """The per-deployment detail response must expose
    ``portfolio_revision_id`` for the same operator-redeploy discoverability
    as the list endpoint."""
    revision_id = uuid4()
    resp = LiveDeploymentStatusResponse(
        id=uuid4(),
        deployment_slug="abc123",
        status="running",
        paper_trading=True,
        portfolio_revision_id=revision_id,
    )
    assert resp.portfolio_revision_id == revision_id
