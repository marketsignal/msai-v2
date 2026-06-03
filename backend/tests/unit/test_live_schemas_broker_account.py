"""Unit tests for the broker-account selector contract on the live schemas.

Task 4 (broker-account-spawn-wiring): the live-deploy request must support
selecting a broker account by id (``broker_account_id``) as an *either/or*
alternative to the legacy ``account_id`` + ``ib_login_key`` pair, and the
live-status / deployment response must surface ``broker_account_id`` so an E2E
caller can observe the deployment↔account linkage through the API (no DB peek).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from msai.schemas.live import LiveDeploymentInfo, PortfolioStartRequest


def test_selector_only_request_is_valid_without_legacy_pair() -> None:
    # Arrange
    revision_id = uuid4()
    broker_account_id = uuid4()

    # Act — broker_account_id alone, account_id/ib_login_key OMITTED
    req = PortfolioStartRequest(
        portfolio_revision_id=revision_id,
        paper_trading=True,
        broker_account_id=broker_account_id,
    )

    # Assert
    assert req.broker_account_id == broker_account_id
    assert req.account_id is None
    assert req.ib_login_key is None


def test_legacy_pair_request_is_still_valid_without_broker_account_id() -> None:
    # Arrange
    revision_id = uuid4()

    # Act — legacy form: account_id + ib_login_key, no broker_account_id
    req = PortfolioStartRequest(
        portfolio_revision_id=revision_id,
        account_id="DU123",
        ib_login_key="key",
        paper_trading=True,
    )

    # Assert
    assert req.broker_account_id is None
    assert req.account_id == "DU123"
    assert req.ib_login_key == "key"


def test_request_with_neither_selector_nor_legacy_pair_raises() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValidationError):
        PortfolioStartRequest(
            portfolio_revision_id=uuid4(),
            paper_trading=True,
        )


def test_request_with_only_account_id_and_no_ib_login_key_raises() -> None:
    # The legacy pair must be BOTH present — account_id alone (no
    # broker_account_id, no ib_login_key) does not satisfy the either/or.
    with pytest.raises(ValidationError):
        PortfolioStartRequest(
            portfolio_revision_id=uuid4(),
            account_id="DU123",
            paper_trading=True,
        )


def test_request_with_all_three_is_accepted_either_or_not_xor() -> None:
    # Arrange
    revision_id = uuid4()
    broker_account_id = uuid4()

    # Act — broker_account_id AND legacy pair (back-compat for callers that
    # send everything). Either/or, NOT exclusive-or.
    req = PortfolioStartRequest(
        portfolio_revision_id=revision_id,
        account_id="DU123",
        ib_login_key="key",
        paper_trading=True,
        broker_account_id=broker_account_id,
    )

    # Assert
    assert req.broker_account_id == broker_account_id
    assert req.account_id == "DU123"
    assert req.ib_login_key == "key"


def test_deployment_info_accepts_and_serializes_broker_account_id() -> None:
    # Arrange
    broker_account_id = uuid4()

    # Act
    info = LiveDeploymentInfo(
        id=uuid4(),
        status="running",
        paper_trading=True,
        broker_account_id=broker_account_id,
    )
    dumped = info.model_dump()

    # Assert
    assert info.broker_account_id == broker_account_id
    assert dumped["broker_account_id"] == broker_account_id


def test_deployment_info_broker_account_id_defaults_to_none() -> None:
    # Act
    info = LiveDeploymentInfo(
        id=uuid4(),
        status="running",
        paper_trading=True,
    )

    # Assert
    assert info.broker_account_id is None
