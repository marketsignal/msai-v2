from msai.core.halt_keys import (
    HaltCause,
    account_halt_key,
    fleet_halt_key,
    halt_cause_key,
)


def test_fleet_halt_key_is_canonical_global_string() -> None:
    assert fleet_halt_key() == "msai:risk:halt"


def test_account_halt_key_namespaces_by_account_id() -> None:
    # Critical: keyed by account_id, not ib_login_key. Two sub-accounts
    # under one TWS login MUST have independent halt latches.
    assert account_halt_key("DUP733214") == "msai:risk:halt:account:DUP733214"
    assert account_halt_key("DUP733215") == "msai:risk:halt:account:DUP733215"


def test_account_halt_key_rejects_empty_account_id() -> None:
    import pytest
    with pytest.raises(ValueError, match="account_id must be non-empty"):
        account_halt_key("")


def test_halt_cause_key_fleet_scope() -> None:
    assert halt_cause_key("fleet") == "msai:risk:halt:cause"


def test_halt_cause_key_account_scope() -> None:
    assert (
        halt_cause_key("account", account_id="DUP733214")
        == "msai:risk:halt:account:DUP733214:cause"
    )


def test_halt_cause_enum_values() -> None:
    assert HaltCause.FLEET_EMERGENCY.value == "fleet_emergency"
    assert HaltCause.DATA_STALE.value == "data_stale"  # forward-compat for PR 1b
    assert HaltCause.OPERATOR_DRAIN.value == "operator_drain"
