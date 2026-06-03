"""Unit tests for the broker-account binding columns on LiveDeployment (PR broker-account-spawn-wiring, Task 2).

Task 1 (migration 81e7efe6d772) added nullable columns to ``live_deployments``:
``broker_account_id`` (FK → broker_accounts.id, ondelete RESTRICT),
``credentials_validated_at`` (tz-aware DateTime), and
``credentials_validated_version`` (String(128)). Task 2 maps these onto the
SQLAlchemy model ADDITIVELY — the legacy ``account_id`` / ``ib_login_key``
columns must remain untouched.
"""

from __future__ import annotations

from uuid import uuid4


def test_live_deployment_has_broker_account_binding_columns() -> None:
    """The three new nullable binding columns exist on the table."""
    from msai.models import LiveDeployment

    cols = {c.name: c for c in LiveDeployment.__table__.columns}

    assert "broker_account_id" in cols
    assert cols["broker_account_id"].nullable is True

    assert "credentials_validated_at" in cols
    assert cols["credentials_validated_at"].nullable is True

    assert "credentials_validated_version" in cols
    assert cols["credentials_validated_version"].nullable is True


def test_broker_account_id_fk_targets_broker_accounts_with_restrict() -> None:
    """broker_account_id is a FK to broker_accounts.id with ondelete RESTRICT."""
    from msai.models import LiveDeployment

    col = LiveDeployment.__table__.columns["broker_account_id"]
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "broker_accounts"
    assert fk.column.name == "id"
    assert fk.ondelete == "RESTRICT"


def test_live_deployment_has_broker_account_relationship() -> None:
    """The ``broker_account`` relationship attribute is configured."""
    from sqlalchemy import inspect

    from msai.models import LiveDeployment

    mapper = inspect(LiveDeployment)
    assert "broker_account" in mapper.relationships
    rel = mapper.relationships["broker_account"]
    assert rel.mapper.class_.__name__ == "BrokerAccount"


def test_live_deployment_keeps_legacy_account_columns() -> None:
    """Additive: legacy account_id / ib_login_key columns are still present."""
    from msai.models import LiveDeployment

    cols = LiveDeployment.__table__.columns.keys()
    assert "account_id" in cols
    assert "ib_login_key" in cols


def test_live_deployment_instance_accepts_new_attributes() -> None:
    """A LiveDeployment can be constructed with the new binding attributes set,
    and a legacy-valid NULL credentials_validated_version is acceptable."""
    from msai.models import LiveDeployment

    broker_account_id = uuid4()
    deployment = LiveDeployment(
        deployment_slug="abcd1234abcd1234",
        identity_signature="0" * 64,
        trader_id="MSAI-abcd1234abcd1234",
        strategy_id_full="EmaCross-abcd1234abcd1234",
        account_id="DU1234567",
        ib_login_key="test-lvp",
        portfolio_revision_id=uuid4(),
        message_bus_stream="trader-MSAI-abcd1234abcd1234-stream",
        broker_account_id=broker_account_id,
        credentials_validated_version=None,
        credentials_validated_at=None,
    )

    # New attributes round-trip on the instance.
    assert deployment.broker_account_id == broker_account_id
    assert deployment.credentials_validated_version is None  # legacy-valid
    assert deployment.credentials_validated_at is None
    # Relationship attribute exists on the instance (unset → None).
    assert deployment.broker_account is None
    # Legacy attributes still present and set.
    assert deployment.account_id == "DU1234567"
    assert deployment.ib_login_key == "test-lvp"
