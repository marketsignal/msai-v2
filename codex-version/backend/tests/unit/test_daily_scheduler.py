from __future__ import annotations

from datetime import UTC, datetime

from msai.core.config import settings
from msai.workers import daily_scheduler


def test_is_due_after_schedule_when_not_already_enqueued(monkeypatch) -> None:
    monkeypatch.setattr(settings, "daily_ingest_hour", 18)
    monkeypatch.setattr(settings, "daily_ingest_minute", 30)

    current = datetime(2026, 4, 7, 18, 45, tzinfo=UTC)

    assert daily_scheduler._is_due(current, None) is True
    assert daily_scheduler._is_due(current, "2026-04-07") is False


def test_is_due_before_schedule_returns_false(monkeypatch) -> None:
    monkeypatch.setattr(settings, "daily_ingest_hour", 18)
    monkeypatch.setattr(settings, "daily_ingest_minute", 30)

    current = datetime(2026, 4, 7, 18, 15, tzinfo=UTC)

    assert daily_scheduler._is_due(current, None) is False
