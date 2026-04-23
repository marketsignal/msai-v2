"""Unit tests for the :mod:`msai.workers.backtest_job` helpers.

Narrow-scope coverage of the module-level near-pure helpers. Caller-side
integration (``_execute_backtest`` → ``_materialize_series_payload`` →
``_finalize_backtest``) is exercised in
``tests/integration/test_backtest_job_auto_heal.py`` by the full
``run_backtest_job`` path.
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest
import structlog.testing

from msai.workers import backtest_job


def test_materialize_series_payload_success_returns_ready() -> None:
    """Happy path: helper returns ``(payload, "ready")`` + emits INFO log.

    The returned payload must round-trip through ``SeriesPayload`` (done
    inside the helper) so a shape regression can't sneak past the
    finalize write.
    """
    # Arrange — small well-formed returns series with UTC DatetimeIndex.
    idx = pd.date_range("2024-01-02", periods=3, freq="B", tz="UTC")
    returns = pd.Series([0.01, -0.005, 0.003], index=idx, name="returns")

    # Act
    with structlog.testing.capture_logs() as captured:
        payload, status = backtest_job._materialize_series_payload(
            returns_series=returns,
            backtest_id="bt-happy",
        )

    # Assert — status + payload shape.
    assert status == "ready"
    assert payload is not None
    assert "daily" in payload
    assert "monthly_returns" in payload
    assert len(payload["daily"]) > 0

    # Assert — INFO audit event emitted (PRD §7 contract: event name +
    # level + payload_bytes field present).
    matches = [
        entry
        for entry in captured
        if entry.get("event") == "backtest_series_materialized" and entry.get("log_level") == "info"
    ]
    assert matches, f"missing backtest_series_materialized INFO log; got {captured!r}"
    assert "payload_bytes" in matches[0]
    assert matches[0]["backtest_id"] == "bt-happy"


def test_materialize_series_payload_failure_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``build_series_payload`` raises, helper fails soft.

    Returns ``(None, "failed")`` + emits a WARNING structured log event
    carrying ``nautilus_version`` (PRD §7 audit field so operators can
    correlate series failures with the engine version that produced the
    account DataFrame).
    """

    # Arrange — replace the import target inside the worker module so
    # the helper's call-site picks up the raising stub.
    def _boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated series-build failure")

    monkeypatch.setattr(backtest_job, "build_series_payload", _boom)

    # Act
    with structlog.testing.capture_logs() as captured:
        payload, status = backtest_job._materialize_series_payload(
            returns_series=pd.Series([0.01], name="returns"),
            backtest_id="bt-fail",
            nautilus_version="1.223.0",
        )

    # Assert — fail-soft tuple.
    assert payload is None
    assert status == "failed"

    # Assert — WARNING audit event with nautilus_version field.
    matches = [
        entry
        for entry in captured
        if entry.get("event") == "backtest_series_materialization_failed"
        and entry.get("log_level") in ("warning", "error")
    ]
    assert matches, f"missing backtest_series_materialization_failed WARNING log; got {captured!r}"
    assert matches[0]["nautilus_version"] == "1.223.0"
    assert matches[0]["backtest_id"] == "bt-fail"


@pytest.mark.parametrize(
    "shutdown_exc",
    [asyncio.CancelledError, KeyboardInterrupt, SystemExit],
)
def test_materialize_series_payload_propagates_shutdown_signals(
    shutdown_exc: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cooperative shutdown signals MUST propagate — a swallowed
    ``asyncio.CancelledError`` would mis-finalize a cancelled run as
    ``status="completed"`` + ``series_status="failed"``. A regression
    that removes the narrow re-raise clause or merges it into
    ``except BaseException`` would silently flip these runs.
    """

    def _raise_shutdown(*_a: object, **_kw: object) -> dict[str, object]:
        raise shutdown_exc()

    monkeypatch.setattr(backtest_job, "build_series_payload", _raise_shutdown)

    with pytest.raises(shutdown_exc):
        backtest_job._materialize_series_payload(
            returns_series=pd.Series([0.01], name="returns"),
            backtest_id="bt-shutdown",
        )
