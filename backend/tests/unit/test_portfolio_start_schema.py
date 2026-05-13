"""Unit tests for the PortfolioStartRequest schema and deployment strategy wiring."""

from uuid import uuid4

import pytest

from msai.models.live_deployment_strategy import LiveDeploymentStrategy
from msai.schemas.live import PortfolioStartRequest
from msai.services.live.deployment_identity import derive_strategy_id_full


def test_portfolio_start_request_requires_revision_id_and_account() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="DU123",
        ib_login_key="user-x",
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
        ib_login_key="user-x",
    )
    assert req.paper_trading is True


def test_portfolio_start_request_paper_trading_override() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="U999",
        paper_trading=False,
        ib_login_key="user-x",
    )
    assert req.paper_trading is False


def test_portfolio_start_request_ib_login_key_required() -> None:
    """Bug #1 (live-deploy-safety-trio): ib_login_key must be required by the
    API schema (the DB column has been NOT NULL since PR #3). Sending the
    request without it should produce a 422 (Pydantic validation error),
    not the previous 500 / IntegrityError surface."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        PortfolioStartRequest(
            portfolio_revision_id=uuid4(),
            account_id="DU123",
        )
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("ib_login_key",) and e["type"] == "missing" for e in errors)


def test_portfolio_start_request_ib_login_key_accepts_value() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="DU123",
        ib_login_key="user-x",
    )
    assert req.ib_login_key == "user-x"


def test_portfolio_start_request_ib_login_key_empty_rejected() -> None:
    """min_length=1 — empty string is rejected."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PortfolioStartRequest(
            portfolio_revision_id=uuid4(),
            account_id="DU123",
            ib_login_key="",
        )


# ---------------------------------------------------------------
# LiveDeploymentStrategy population tests (Task 9)
# ---------------------------------------------------------------


class TestLiveDeploymentStrategyModel:
    """Verify the LiveDeploymentStrategy model has the right columns
    and that derive_strategy_id_full produces the expected format."""

    def test_model_has_required_columns(self) -> None:
        cols = {c.name for c in LiveDeploymentStrategy.__table__.columns}
        assert "deployment_id" in cols
        assert "revision_strategy_id" in cols
        assert "strategy_id_full" in cols
        assert "created_at" in cols

    def test_model_columns_not_nullable(self) -> None:
        col_map = {c.name: c for c in LiveDeploymentStrategy.__table__.columns}
        assert col_map["deployment_id"].nullable is False
        assert col_map["revision_strategy_id"].nullable is False
        assert col_map["strategy_id_full"].nullable is False

    def test_derive_strategy_id_full_format(self) -> None:
        """strategy_id_full = '{class}-{order_index}-{slug}'."""
        result = derive_strategy_id_full("EMACross", "abc123", 0)
        assert result == "EMACross-0-abc123"

    def test_derive_strategy_id_full_with_order_index(self) -> None:
        result = derive_strategy_id_full("EMACross", "abc123", 2)
        assert result == "EMACross-2-abc123"

    def test_lds_instantiation_with_derived_id(self) -> None:
        """Simulate what /start-portfolio does: create LDS with derived strategy_id_full."""
        deployment_id = uuid4()
        member_id = uuid4()
        strategy_id_full = derive_strategy_id_full("SmokeMarketOrder", "slug1234", 0)
        lds = LiveDeploymentStrategy(
            deployment_id=deployment_id,
            revision_strategy_id=member_id,
            strategy_id_full=strategy_id_full,
        )
        assert lds.deployment_id == deployment_id
        assert lds.revision_strategy_id == member_id
        assert lds.strategy_id_full == "SmokeMarketOrder-0-slug1234"

    def test_lds_multiple_members_get_distinct_strategy_ids(self) -> None:
        """Portfolio with 3 members should produce 3 distinct strategy_id_full values."""
        deployment_id = uuid4()
        slug = "testslug"
        members_data = [
            ("EMACross", 0),
            ("EMACross", 1),
            ("SmokeMarketOrder", 2),
        ]
        ids = set()
        for class_name, order_index in members_data:
            sid = derive_strategy_id_full(class_name, slug, order_index)
            ids.add(sid)
        assert len(ids) == 3
        assert "EMACross-0-testslug" in ids
        assert "EMACross-1-testslug" in ids
        assert "SmokeMarketOrder-2-testslug" in ids
