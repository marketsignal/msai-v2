"""Unit tests for the new multi-login routing columns (PR#1)."""

from __future__ import annotations


def test_live_deployment_has_ib_login_key_column() -> None:
    from msai.models import LiveDeployment

    cols = {c.name: c for c in LiveDeployment.__table__.columns}
    assert "ib_login_key" in cols
    assert cols["ib_login_key"].nullable is True


def test_live_node_process_has_gateway_session_key_column() -> None:
    from msai.models import LiveNodeProcess

    cols = {c.name: c for c in LiveNodeProcess.__table__.columns}
    assert "gateway_session_key" in cols
    assert cols["gateway_session_key"].nullable is True
