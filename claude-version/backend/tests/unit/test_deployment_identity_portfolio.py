"""Unit tests for portfolio-based deployment identity (Tasks 4 + 19).

Task 4: PortfolioDeploymentIdentity dataclass + derive helper.
Task 19: derive_strategy_id_full includes order_index to disambiguate
         same-class portfolio members.
"""

from __future__ import annotations

from uuid import uuid4

from msai.services.live.deployment_identity import (
    PortfolioDeploymentIdentity,
    derive_portfolio_deployment_identity,
    derive_strategy_id_full,
)


# ---------------------------------------------------------------------------
# Task 4: PortfolioDeploymentIdentity
# ---------------------------------------------------------------------------


class TestPortfolioDeploymentIdentity:
    def test_signature_deterministic(self) -> None:
        rev_id = uuid4()
        id1 = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id=rev_id.hex,
            account_id="DU123",
            paper_trading=True,
        )
        id2 = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id=rev_id.hex,
            account_id="DU123",
            paper_trading=True,
        )
        assert id1.signature() == id2.signature()

    def test_different_revision_produces_different_signature(self) -> None:
        id1 = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id=uuid4().hex,
            account_id="DU123",
            paper_trading=True,
        )
        id2 = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id=uuid4().hex,
            account_id="DU123",
            paper_trading=True,
        )
        assert id1.signature() != id2.signature()

    def test_different_account_produces_different_signature(self) -> None:
        rev_id = uuid4()
        id1 = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id=rev_id.hex,
            account_id="DU123",
            paper_trading=True,
        )
        id2 = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id=rev_id.hex,
            account_id="DU456",
            paper_trading=True,
        )
        assert id1.signature() != id2.signature()

    def test_different_paper_trading_produces_different_signature(self) -> None:
        rev_id = uuid4()
        id1 = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id=rev_id.hex,
            account_id="DU123",
            paper_trading=True,
        )
        id2 = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id=rev_id.hex,
            account_id="DU123",
            paper_trading=False,
        )
        assert id1.signature() != id2.signature()

    def test_canonical_json_sorts_keys(self) -> None:
        identity = PortfolioDeploymentIdentity(
            started_by="abc",
            portfolio_revision_id="deadbeef",
            account_id="DU123",
            paper_trading=True,
        )
        canonical = identity.to_canonical_json().decode("utf-8")
        keys = ["account_id", "paper_trading", "portfolio_revision_id", "started_by"]
        positions = [canonical.index(f'"{k}":') for k in keys]
        assert positions == sorted(positions)


class TestDerivePortfolioDeploymentIdentity:
    def test_builds_correct_fields(self) -> None:
        rev_id = uuid4()
        user_id = uuid4()
        identity = derive_portfolio_deployment_identity(
            user_id=user_id,
            portfolio_revision_id=rev_id,
            account_id="DU123",
            paper_trading=False,
        )
        assert identity.portfolio_revision_id == rev_id.hex
        assert identity.account_id == "DU123"
        assert identity.paper_trading is False
        assert identity.started_by == user_id.hex

    def test_null_user_id_canonicalizes_to_empty_string(self) -> None:
        rev_id = uuid4()
        identity = derive_portfolio_deployment_identity(
            user_id=None,
            portfolio_revision_id=rev_id,
            account_id="DU123",
            paper_trading=True,
        )
        assert identity.started_by == ""

    def test_user_sub_fallback(self) -> None:
        rev_id = uuid4()
        identity = derive_portfolio_deployment_identity(
            user_id=None,
            portfolio_revision_id=rev_id,
            account_id="DU123",
            paper_trading=True,
            user_sub="alice@example.com",
        )
        assert identity.started_by == "sub:alice@example.com"


# ---------------------------------------------------------------------------
# Task 19: derive_strategy_id_full with order_index
# ---------------------------------------------------------------------------


class TestDeriveStrategyIdFullWithOrderIndex:
    def test_includes_order_index(self) -> None:
        result = derive_strategy_id_full("EMACross", "abc123", order_index=0)
        assert result == "EMACross-0-abc123"

    def test_different_order_index_produces_different_id(self) -> None:
        id1 = derive_strategy_id_full("EMACross", "abc123", order_index=0)
        id2 = derive_strategy_id_full("EMACross", "abc123", order_index=1)
        assert id1 != id2

    def test_backward_compat_default_order_index(self) -> None:
        # Default order_index=0 for single-strategy backward compat
        result = derive_strategy_id_full("EMACross", "abc123")
        assert result == "EMACross-0-abc123"

    def test_higher_order_index(self) -> None:
        result = derive_strategy_id_full("BollingerBand", "deadbeef", order_index=5)
        assert result == "BollingerBand-5-deadbeef"
