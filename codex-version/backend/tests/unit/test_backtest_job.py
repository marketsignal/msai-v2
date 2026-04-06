from __future__ import annotations

from datetime import UTC

from msai.workers.backtest_job import _trade_timestamp_utc


def test_trade_timestamp_uses_ts_event_fallback() -> None:
    trade = {"ts_event": "2024-02-01T14:30:00Z"}
    timestamp = _trade_timestamp_utc(trade)

    assert timestamp.tzinfo is UTC
    assert timestamp.isoformat() == "2024-02-01T14:30:00+00:00"


def test_trade_timestamp_returns_now_when_no_supported_keys() -> None:
    trade = {"id": "abc"}
    timestamp = _trade_timestamp_utc(trade)

    assert timestamp.tzinfo is UTC
