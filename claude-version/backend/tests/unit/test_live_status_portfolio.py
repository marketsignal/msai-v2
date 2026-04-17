"""Tests for portfolio deployment response schemas."""

from __future__ import annotations

from uuid import uuid4

from msai.schemas.live import PortfolioDeploymentInfo, StrategyMemberInfo


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
