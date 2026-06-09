"""Unit tests for broker-account Pydantic schemas.

Security-critical invariant: ``BrokerAccountResponse`` must never expose the
TWS credential fields (``tws_userid`` / ``tws_password``). Credentials live in
the secrets backend; the response carries only metadata references.
"""


def test_response_schema_has_no_secret_fields() -> None:
    from msai.schemas.broker_account import BrokerAccountResponse

    fields = set(BrokerAccountResponse.model_fields)
    assert "tws_userid" not in fields and "tws_password" not in fields
    for meta in ("credentials_secret_ref", "credentials_secret_version", "credentials_updated_at"):
        assert meta in fields


def test_create_request_requires_credentials() -> None:
    from msai.schemas.broker_account import BrokerAccountCreateRequest

    f = BrokerAccountCreateRequest.model_fields
    assert "tws_userid" in f and "tws_password" in f and "ib_account_id" in f


def test_create_request_rejects_blank_ib_login_key() -> None:
    # finding 2: a blank/whitespace-only ib_login_key is invalid (the schema is
    # the authoritative boundary — min_length=1 + strip/non-blank validator).
    import pytest
    from pydantic import ValidationError

    from msai.schemas.broker_account import BrokerAccountCreateRequest

    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            BrokerAccountCreateRequest(
                ib_account_id="DU1",
                ib_login_key=bad,
                trading_mode="paper",
                tws_userid="u",
                tws_password="p",
            )


def test_create_request_strips_ib_login_key() -> None:
    # the validator normalizes surrounding whitespace so the stored routing key
    # matches what the GatewayRouter resolves.
    from msai.schemas.broker_account import BrokerAccountCreateRequest

    req = BrokerAccountCreateRequest(
        ib_account_id="DU1",
        ib_login_key="  lvp  ",
        trading_mode="paper",
        tws_userid="u",
        tws_password="p",
    )
    assert req.ib_login_key == "lvp"


# finding 1 (iter-3 P2): cross-field guard mirrors the live-start / CLI
# DU/DF-vs-U prefix rule (ib_port_validator.IB_PAPER_PREFIXES = ("DU", "DF")).
# Paper accounts are DU*/DF* prefixed; live accounts start with U (NOT DU/DF).
# Closes nautilus gotcha #6 at the broker-account boundary.


def test_create_request_rejects_live_account_with_paper_mode() -> None:
    import pytest
    from pydantic import ValidationError

    from msai.schemas.broker_account import BrokerAccountCreateRequest

    with pytest.raises(ValidationError):
        BrokerAccountCreateRequest(
            ib_account_id="U4705114",
            ib_login_key="lvp",
            trading_mode="paper",
            tws_userid="u",
            tws_password="p",
        )


def test_create_request_rejects_paper_account_with_live_mode() -> None:
    import pytest
    from pydantic import ValidationError

    from msai.schemas.broker_account import BrokerAccountCreateRequest

    with pytest.raises(ValidationError):
        BrokerAccountCreateRequest(
            ib_account_id="DU1234567",
            ib_login_key="lvp",
            trading_mode="live",
            tws_userid="u",
            tws_password="p",
        )


def test_create_request_accepts_paper_account_with_paper_mode() -> None:
    from msai.schemas.broker_account import BrokerAccountCreateRequest

    for acct in ("DU1234567", "DF1234567"):
        req = BrokerAccountCreateRequest(
            ib_account_id=acct,
            ib_login_key="lvp",
            trading_mode="paper",
            tws_userid="u",
            tws_password="p",
        )
        assert req.trading_mode == "paper"


def test_create_request_accepts_live_account_with_live_mode() -> None:
    from msai.schemas.broker_account import BrokerAccountCreateRequest

    req = BrokerAccountCreateRequest(
        ib_account_id="U4705114",
        ib_login_key="lvp",
        trading_mode="live",
        tws_userid="u",
        tws_password="p",
    )
    assert req.trading_mode == "live"


# PR4 (dashboard-account-selector): account_class on response + create.


def test_response_carries_account_class_and_is_real_money() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from msai.models.broker_account import AccountClass, BrokerAccount
    from msai.schemas.broker_account import BrokerAccountResponse

    acct = BrokerAccount(
        id=uuid4(),
        ib_account_id="U4715997",
        ib_login_key="mshvp000",
        label="HVP",
        status="active",
        gateway_slot="slot-1",
        trading_mode="live",
        account_class=AccountClass.REAL,
        credentials_backend="env",
        credentials_secret_ref="ref",
        credentials_secret_version=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    resp = BrokerAccountResponse.model_validate(acct)
    assert resp.account_class == "real"
    assert resp.is_real_money is True


def test_create_defaults_account_class_from_trading_mode() -> None:
    from msai.schemas.broker_account import BrokerAccountCreateRequest

    paper = BrokerAccountCreateRequest(
        ib_account_id="DU123",
        ib_login_key="k",
        trading_mode="paper",
        tws_userid="u",
        tws_password="p",
    )
    assert paper.account_class == "paper"
    live = BrokerAccountCreateRequest(
        ib_account_id="U123",
        ib_login_key="k",
        trading_mode="live",
        tws_userid="u",
        tws_password="p",
    )
    assert live.account_class == "test"


def test_create_rejects_real_class_on_paper_account() -> None:
    import pytest
    from pydantic import ValidationError

    from msai.schemas.broker_account import BrokerAccountCreateRequest

    with pytest.raises(ValidationError):
        BrokerAccountCreateRequest(
            ib_account_id="DU123",
            ib_login_key="k",
            trading_mode="paper",
            account_class="real",
            tws_userid="u",
            tws_password="p",
        )


def test_create_rejects_contradictory_class_mode_pairs() -> None:
    """Codex code-review iter-2 P2: the full class/mode matrix is enforced —
    paper mode must be account_class='paper'; live mode must be 'test'|'real'.
    A live+paper or paper+test pair is a contradictory taxonomy → 422."""
    import pytest
    from pydantic import ValidationError

    from msai.schemas.broker_account import BrokerAccountCreateRequest

    # live account labeled 'paper' — rejected
    with pytest.raises(ValidationError):
        BrokerAccountCreateRequest(
            ib_account_id="U123",
            ib_login_key="k",
            trading_mode="live",
            account_class="paper",
            tws_userid="u",
            tws_password="p",
        )
    # paper account labeled 'test' — rejected
    with pytest.raises(ValidationError):
        BrokerAccountCreateRequest(
            ib_account_id="DU123",
            ib_login_key="k",
            trading_mode="paper",
            account_class="test",
            tws_userid="u",
            tws_password="p",
        )
