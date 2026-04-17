"""Unit tests for the PortfolioStartRequest schema."""

from uuid import uuid4

import pytest

from msai.schemas.live import PortfolioStartRequest


def test_portfolio_start_request_requires_revision_id_and_account() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="DU123",
    )
    assert req.paper_trading is True


def test_portfolio_start_request_rejects_missing_revision_id() -> None:
    with pytest.raises(Exception):
        PortfolioStartRequest(account_id="DU123")  # type: ignore[call-arg]


def test_portfolio_start_request_rejects_missing_account_id() -> None:
    with pytest.raises(Exception):
        PortfolioStartRequest(portfolio_revision_id=uuid4())  # type: ignore[call-arg]


def test_portfolio_start_request_accepts_ib_login_key() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="DU123",
        ib_login_key="marin1016test",
    )
    assert req.ib_login_key == "marin1016test"


def test_portfolio_start_request_paper_trading_defaults_true() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="DU999",
    )
    assert req.paper_trading is True


def test_portfolio_start_request_paper_trading_override() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="U999",
        paper_trading=False,
    )
    assert req.paper_trading is False


def test_portfolio_start_request_ib_login_key_defaults_none() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="DU123",
    )
    assert req.ib_login_key is None
