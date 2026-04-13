"""Tests for backtest lifecycle fields (Task 1 of hybrid merge)."""

from __future__ import annotations

from msai.models.backtest import Backtest


def test_backtest_has_lifecycle_columns() -> None:
    """All 5 lifecycle columns exist on the Backtest model."""
    table = Backtest.__table__
    expected = {"queue_name", "queue_job_id", "worker_id", "attempt", "heartbeat_at"}
    actual = {c.name for c in table.columns}
    assert expected.issubset(actual), f"Missing columns: {expected - actual}"


def test_attempt_defaults_to_zero() -> None:
    """The attempt column defaults to 0 (not 1)."""
    col = Backtest.__table__.columns["attempt"]
    assert col.default is not None
    assert col.default.arg == 0


def test_lifecycle_fields_are_nullable() -> None:
    """queue_name, queue_job_id, worker_id, heartbeat_at are nullable."""
    table = Backtest.__table__
    for name in ("queue_name", "queue_job_id", "worker_id", "heartbeat_at"):
        assert table.columns[name].nullable is True, f"{name} should be nullable"


def test_attempt_is_not_nullable() -> None:
    """attempt is NOT nullable."""
    col = Backtest.__table__.columns["attempt"]
    assert col.nullable is False
