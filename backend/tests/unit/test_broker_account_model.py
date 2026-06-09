from msai.models.broker_account import BrokerAccount, BrokerAccountStatus, CredentialsBackend


def test_broker_account_status_values():
    assert BrokerAccountStatus.ACTIVE == "active"
    assert BrokerAccountStatus.ARCHIVED == "archived"


def test_credentials_backend_values():
    assert CredentialsBackend.AZURE_KV == "azure_kv"
    assert CredentialsBackend.ENV == "env"
    assert CredentialsBackend.LEGACY_ENV == "legacy_env"


def test_broker_account_tablename_and_columns():
    assert BrokerAccount.__tablename__ == "broker_accounts"
    cols = BrokerAccount.__table__.columns.keys()
    for required in (
        "id",
        "ib_account_id",
        "ib_login_key",
        "label",
        "status",
        "gateway_slot",
        "trading_mode",
        "credentials_backend",
        "credentials_secret_ref",
        "credentials_secret_version",
        "credentials_updated_at",
        "credentials_updated_by",
        "credentials_last_accessed",
        "created_by",
        "created_at",
        "updated_at",
    ):
        assert required in cols, f"missing column {required}"


def test_account_class_column_present_and_string_backed() -> None:
    from msai.models.broker_account import BrokerAccount

    col = {c.name: c for c in BrokerAccount.__table__.columns}["account_class"]
    assert col.nullable is False
    # String-backed (NOT native PG enum) — matches status/trading_mode convention.
    assert col.type.__class__.__name__ == "String"
    assert col.server_default is not None  # existing rows backfill to the default


def test_is_real_money_only_true_for_real_class() -> None:
    from msai.models.broker_account import AccountClass, BrokerAccount

    real = BrokerAccount(account_class=AccountClass.REAL)
    test = BrokerAccount(account_class=AccountClass.TEST)
    paper = BrokerAccount(account_class=AccountClass.PAPER)
    assert real.is_real_money is True
    assert test.is_real_money is False
    assert paper.is_real_money is False
