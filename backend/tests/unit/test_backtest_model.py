"""Tests for the Backtest model's new error classification columns."""

from __future__ import annotations

from msai.models.backtest import Backtest


def test_backtest_has_error_code_column():
    assert hasattr(Backtest, "error_code")
    assert Backtest.__table__.c.error_code.type.length == 32
    assert not Backtest.__table__.c.error_code.nullable
    assert Backtest.__table__.c.error_code.server_default.arg == "unknown"


def test_backtest_has_error_public_message_column():
    assert hasattr(Backtest, "error_public_message")
    assert Backtest.__table__.c.error_public_message.nullable is True


def test_backtest_has_error_suggested_action_column():
    assert hasattr(Backtest, "error_suggested_action")
    assert Backtest.__table__.c.error_suggested_action.nullable is True


def test_backtest_has_error_remediation_column():
    assert hasattr(Backtest, "error_remediation")
    # JSONB subtype
    assert Backtest.__table__.c.error_remediation.nullable is True
    assert "JSONB" in str(Backtest.__table__.c.error_remediation.type)
