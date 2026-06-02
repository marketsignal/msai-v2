"""Unit tests for the 422 validation-input credential redactor.

``_redact_validation_input`` masks sensitive credential material
(``tws_password`` / ``tws_userid``) echoed back in Pydantic v2 validation
errors. Pydantic includes the raw rejected ``input`` in every error item — for
a model-level (``loc=("body",)``) error that ``input`` is the ENTIRE request
body dict, which carries the cleartext credentials. The redactor must strip
those at any depth (iter-5 P3-a: robust to future nested / list schemas).
"""

from __future__ import annotations

from msai.main import _REDACTED, _redact_validation_input


def test_top_level_dict_masks_sensitive_keys() -> None:
    body = {"tws_userid": "secretuser", "tws_password": "secretpw", "ib_account_id": "U123"}
    out = _redact_validation_input(("body",), body)
    assert out["tws_userid"] == _REDACTED
    assert out["tws_password"] == _REDACTED
    assert out["ib_account_id"] == "U123"  # non-sensitive passes through


def test_scalar_input_masked_when_loc_names_sensitive_field() -> None:
    # Field-level error: loc names the sensitive field, input is the scalar.
    assert _redact_validation_input(("body", "tws_password"), "cleartext") == _REDACTED
    assert _redact_validation_input(("body", "ib_login_key"), "lvp") == "lvp"


def test_nested_dict_masks_sensitive_keys_at_depth() -> None:
    # A future nested credential schema must not leak buried secrets.
    body = {
        "account": {
            "ib_account_id": "U123",
            "credentials": {"tws_userid": "secretuser", "tws_password": "secretpw"},
        }
    }
    out = _redact_validation_input(("body",), body)
    creds = out["account"]["credentials"]
    assert creds["tws_userid"] == _REDACTED
    assert creds["tws_password"] == _REDACTED
    assert out["account"]["ib_account_id"] == "U123"


def test_list_of_dicts_masks_sensitive_keys() -> None:
    # A future bulk/list request shape must not leak secrets inside list items.
    body = {
        "accounts": [
            {"ib_account_id": "U1", "tws_password": "pw1"},
            {"ib_account_id": "U2", "tws_password": "pw2", "tws_userid": "uu2"},
        ]
    }
    out = _redact_validation_input(("body",), body)
    assert out["accounts"][0]["tws_password"] == _REDACTED
    assert out["accounts"][0]["ib_account_id"] == "U1"
    assert out["accounts"][1]["tws_password"] == _REDACTED
    assert out["accounts"][1]["tws_userid"] == _REDACTED


def test_non_sensitive_scalar_passes_through_unchanged() -> None:
    assert _redact_validation_input(("body", "label"), "my-label") == "my-label"
    assert _redact_validation_input(("body",), 42) == 42


def test_credential_alias_keys_are_redacted() -> None:
    # Codex iter-10 P2: a credential-looking ALIAS/casing-variant key (e.g. an extra
    # `twsPassword` a client sends alongside the real field) must also be masked in a
    # model-level body echo — not just the exact `tws_password`/`tws_userid` names.
    body = {
        "ib_account_id": "U123",
        "ib_login_key": "lvp",  # NOT a credential — must pass through
        "trading_mode": "paper",
        "tws_userid": "user",
        "tws_password": "secretpw",
        "twsPassword": "aliasleak",  # camelCase alias extra
        "password": "barepwleak",  # bare alias extra
        "twsUserid": "aliasuser",  # camelCase userid alias
        "tws_user_id": "underscoreuser",  # snake_case underscore variant (Codex iter-12)
        "pass_word": "underscorepw",  # underscore-split password
        "tws-userid": "hyphenuser",  # hyphen variant
        "twsUsername": "usernameleak",  # camelCase username alias (Codex final2 P2)
        "username": "bareusernameleak",  # bare username alias
    }
    out = _redact_validation_input(("body",), body)
    for k in (
        "tws_password",
        "tws_userid",
        "twsPassword",
        "password",
        "twsUserid",
        "tws_user_id",
        "pass_word",
        "tws-userid",
        "twsUsername",
        "username",
    ):
        assert out[k] == _REDACTED, f"{k} should be redacted"
    # benign fields untouched (ib_login_key must NOT be redacted — it's returned in responses)
    assert out["ib_login_key"] == "lvp"
    assert out["trading_mode"] == "paper"
    assert out["ib_account_id"] == "U123"
    # no cleartext credential value survives anywhere in the echo (all separator/case variants)
    for leaked in (
        "aliasleak",
        "barepwleak",
        "secretpw",
        "underscoreuser",
        "underscorepw",
        "hyphenuser",
        "usernameleak",
        "bareusernameleak",
    ):
        assert leaked not in str(out), f"{leaked} leaked"
