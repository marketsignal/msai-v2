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
