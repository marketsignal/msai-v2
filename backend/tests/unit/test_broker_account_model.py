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
