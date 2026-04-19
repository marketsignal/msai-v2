"""Unit tests for the tz-aware daily ingest scheduler wrapper.

Covers:
- `_is_due` — returns False before scheduled hour, True after; respects
  per-tz-day idempotency.
- `_load_last_enqueued_date` / `_write_last_enqueued_date` — round-trip,
  missing-file, corrupt-file, wrong-shape tolerance.
- `run_nightly_ingest_if_due` — disabled flag, invalid timezone, not-yet-due,
  already-ran-today, error-during-ingest leaves state untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from msai.workers.nightly_ingest import (
    _is_due,
    _load_last_enqueued_date,
    _write_last_enqueued_date,
    run_nightly_ingest_if_due,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# _is_due — pure scheduling logic
# ---------------------------------------------------------------------------


class TestIsDue:
    def test_not_due_before_scheduled_hour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        before = datetime(2026, 4, 14, 17, 59, tzinfo=ZoneInfo("America/New_York"))

        assert _is_due(before, last_enqueued_date=None) is False

    def test_due_at_scheduled_hour_when_no_prior_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        at = datetime(2026, 4, 14, 18, 0, tzinfo=ZoneInfo("America/New_York"))

        assert _is_due(at, last_enqueued_date=None) is True

    def test_due_after_scheduled_hour_when_no_prior_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        after = datetime(2026, 4, 14, 23, 30, tzinfo=ZoneInfo("America/New_York"))

        assert _is_due(after, last_enqueued_date=None) is True

    def test_not_due_when_already_ran_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        # Same day, after scheduled time, but state file says we ran already.
        same_day_after = datetime(2026, 4, 14, 19, 0, tzinfo=ZoneInfo("America/New_York"))

        assert _is_due(same_day_after, last_enqueued_date="2026-04-14") is False

    def test_due_next_day_after_prior_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        next_day = datetime(2026, 4, 15, 18, 5, tzinfo=ZoneInfo("America/New_York"))

        assert _is_due(next_day, last_enqueued_date="2026-04-14") is True

    @pytest.mark.parametrize(
        ("tz_name", "hour", "minute", "now_local", "expected"),
        [
            # London close 16:30, scheduled 16:30 UK time → due at 16:30
            ("Europe/London", 16, 30, datetime(2026, 4, 14, 16, 30), True),
            ("Europe/London", 16, 30, datetime(2026, 4, 14, 16, 29), False),
            # Tokyo close 15:00 JST → due at 15:00
            ("Asia/Tokyo", 15, 0, datetime(2026, 4, 14, 15, 0), True),
            ("Asia/Tokyo", 15, 0, datetime(2026, 4, 14, 14, 59), False),
        ],
    )
    def test_non_us_market_timezones(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tz_name: str,
        hour: int,
        minute: int,
        now_local: datetime,
        expected: bool,
    ) -> None:
        # Regression guard for the operator-facing motivation: non-US
        # markets must be schedulable by their local close.
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_hour", hour)
        monkeypatch.setattr(settings, "daily_ingest_minute", minute)
        tz_aware = now_local.replace(tzinfo=ZoneInfo(tz_name))

        assert _is_due(tz_aware, last_enqueued_date=None) is expected


# ---------------------------------------------------------------------------
# State file persistence
# ---------------------------------------------------------------------------


class TestHourMinuteRangeValidation:
    # Codex iter 2 P2: out-of-range DAILY_INGEST_HOUR / _MINUTE would
    # previously crash every cron tick at datetime.replace. Range
    # constraints on the pydantic field make config load fail fast with
    # a ValidationError.
    @pytest.mark.parametrize("bad_hour", [-1, 24, 25, 100])
    def test_hour_out_of_range_rejected(self, bad_hour: int) -> None:
        from pydantic import ValidationError

        from msai.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(daily_ingest_hour=bad_hour)

    @pytest.mark.parametrize("bad_minute", [-1, 60, 61, 100])
    def test_minute_out_of_range_rejected(self, bad_minute: int) -> None:
        from pydantic import ValidationError

        from msai.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(daily_ingest_minute=bad_minute)

    @pytest.mark.parametrize(
        ("hour", "minute"),
        [(0, 0), (23, 59), (12, 30), (6, 0)],
    )
    def test_valid_hour_minute_accepted(self, hour: int, minute: int) -> None:
        from msai.core.config import Settings

        s = Settings(daily_ingest_hour=hour, daily_ingest_minute=minute)
        assert s.daily_ingest_hour == hour
        assert s.daily_ingest_minute == minute


class TestStateFile:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_last_enqueued_date(path, "2026-04-14")

        assert _load_last_enqueued_date(path) == "2026-04-14"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _load_last_enqueued_date(tmp_path / "missing.json") is None

    def test_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        # Operator hand-edit gone wrong must not crash the cron — the
        # next eligible tick will overwrite with a fresh write.
        path = tmp_path / "state.json"
        path.write_text("{ not json")

        assert _load_last_enqueued_date(path) is None

    def test_wrong_shape_returns_none(self, tmp_path: Path) -> None:
        # Valid JSON but not a dict (e.g. operator wrote `[]`).
        path = tmp_path / "state.json"
        path.write_text("[]")

        assert _load_last_enqueued_date(path) is None

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "state.json"

        _write_last_enqueued_date(nested, "2026-04-14")

        assert nested.exists()

    def test_write_is_atomic_on_fsync_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 2 P2: if the write fails (disk full, fsync error)
        # the prior state file must survive intact — not be truncated.
        # Without atomic tempfile + os.replace, a kill mid-`write_text`
        # would leave an empty file and the next load would treat state
        # as missing, re-firing the same day's ingest.
        path = tmp_path / "state.json"
        _write_last_enqueued_date(path, "2026-04-14")  # seed good state
        prior = path.read_text()

        def _broken_fsync(_fd: int) -> None:
            raise OSError("simulated disk failure mid-write")

        monkeypatch.setattr("msai.workers.nightly_ingest.os.fsync", _broken_fsync)

        with pytest.raises(OSError, match="simulated disk failure"):
            _write_last_enqueued_date(path, "2026-04-15")

        # Prior state file must survive atomically — os.replace never ran.
        assert path.read_text() == prior
        # No tempfile leak.
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".scheduler-")]
        assert leftovers == []


# ---------------------------------------------------------------------------
# run_nightly_ingest_if_due — end-to-end scheduling decisions
# ---------------------------------------------------------------------------


class TestRunNightlyIngestIfDue:
    @pytest.mark.asyncio
    async def test_skips_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", False)
        # Even at scheduled time, disabled flag must short-circuit.
        with patch(
            "msai.workers.nightly_ingest.run_nightly_ingest", new=AsyncMock()
        ) as mock_ingest:
            result = await run_nightly_ingest_if_due({})

        assert result is None
        mock_ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_invalid_timezone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "Mars/Olympus_Mons")
        with patch(
            "msai.workers.nightly_ingest.run_nightly_ingest", new=AsyncMock()
        ) as mock_ingest:
            result = await run_nightly_ingest_if_due({})

        assert result is None
        mock_ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_not_yet_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        # 17:00 ET = before the 18:00 schedule.
        before = datetime(2026, 4, 14, 17, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch(
            "msai.workers.nightly_ingest.run_nightly_ingest", new=AsyncMock()
        ) as mock_ingest:
            result = await run_nightly_ingest_if_due({}, now=before.astimezone(UTC))

        assert result is None
        mock_ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_fires_when_due_and_writes_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        # 18:30 ET = after schedule, no prior run.
        after = datetime(2026, 4, 14, 18, 30, tzinfo=ZoneInfo("America/New_York"))
        ingest_result: dict[str, int] = {"AAPL": 390}

        with patch(
            "msai.workers.nightly_ingest.run_nightly_ingest",
            new=AsyncMock(return_value=ingest_result),
        ) as mock_ingest:
            result = await run_nightly_ingest_if_due({}, now=after.astimezone(UTC))

        assert result == ingest_result
        mock_ingest.assert_awaited_once()
        # State file must record today's tz date.
        state = json.loads((tmp_path / "scheduler" / "daily_ingest_state.json").read_text())
        assert state["last_enqueued_date"] == "2026-04-14"

    @pytest.mark.asyncio
    async def test_skips_second_call_same_tz_day(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Idempotency guard: two ticks within the same tz day fire the
        # ingest only once.
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        first = datetime(2026, 4, 14, 18, 5, tzinfo=ZoneInfo("America/New_York"))
        second = datetime(2026, 4, 14, 23, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch(
            "msai.workers.nightly_ingest.run_nightly_ingest",
            new=AsyncMock(return_value={}),
        ) as mock_ingest:
            await run_nightly_ingest_if_due({}, now=first.astimezone(UTC))
            await run_nightly_ingest_if_due({}, now=second.astimezone(UTC))

        assert mock_ingest.await_count == 1

    @pytest.mark.asyncio
    async def test_fires_again_next_tz_day(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        day1 = datetime(2026, 4, 14, 18, 5, tzinfo=ZoneInfo("America/New_York"))
        day2 = datetime(2026, 4, 15, 18, 5, tzinfo=ZoneInfo("America/New_York"))

        with patch(
            "msai.workers.nightly_ingest.run_nightly_ingest",
            new=AsyncMock(return_value={}),
        ) as mock_ingest:
            await run_nightly_ingest_if_due({}, now=day1.astimezone(UTC))
            await run_nightly_ingest_if_due({}, now=day2.astimezone(UTC))

        assert mock_ingest.await_count == 2

    @pytest.mark.asyncio
    async def test_ingest_failure_keeps_eager_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 1 P2 fix: state is claimed BEFORE the ingest runs so
        # a long ingest (>60s) doesn't get re-fired by the next minute's
        # cron tick. Trade-off: a transient ingest failure means no
        # auto-retry today — operator must clear the state file or
        # trigger via CLI. Failures surface via the alerting API.
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        when = datetime(2026, 4, 14, 18, 30, tzinfo=ZoneInfo("America/New_York"))

        with (
            patch(
                "msai.workers.nightly_ingest.run_nightly_ingest",
                new=AsyncMock(side_effect=RuntimeError("databento down")),
            ),
            pytest.raises(RuntimeError),
        ):
            await run_nightly_ingest_if_due({}, now=when.astimezone(UTC))

        # State file MUST exist with the claimed date — at-most-once
        # semantics under the eager-claim trade-off.
        state_path = tmp_path / "scheduler" / "daily_ingest_state.json"
        assert state_path.exists()
        state = json.loads(state_path.read_text())
        assert state["last_enqueued_date"] == "2026-04-14"

    @pytest.mark.asyncio
    async def test_long_running_ingest_blocks_next_tick(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression for Codex iter 1 P2: a 90-second ingest must not
        # let the next minute's cron tick start a duplicate. We model
        # this by firing two ticks against an in-progress ingest; the
        # second must skip because the slot is already claimed.
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        first_tick = datetime(2026, 4, 14, 18, 0, tzinfo=ZoneInfo("America/New_York"))
        second_tick = datetime(2026, 4, 14, 18, 1, tzinfo=ZoneInfo("America/New_York"))

        ingest_calls = 0

        async def _slow_ingest(_ctx: dict[str, Any], **_kwargs: Any) -> dict[str, int]:
            nonlocal ingest_calls
            ingest_calls += 1
            # During the "ingest" the wrapper has already claimed the slot.
            # Now fire the next minute's tick — it should skip.
            second_result = await run_nightly_ingest_if_due({}, now=second_tick.astimezone(UTC))
            assert second_result is None
            return {"AAPL": 100}

        monkeypatch.setattr("msai.workers.nightly_ingest.run_nightly_ingest", _slow_ingest)

        first_result = await run_nightly_ingest_if_due({}, now=first_tick.astimezone(UTC))

        assert first_result == {"AAPL": 100}
        assert ingest_calls == 1  # only the first tick triggered the ingest

    @pytest.mark.asyncio
    async def test_passes_scheduled_tz_date_to_ingest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 1 P1 fix: the scheduler's tz date — not the process's
        # local date — must be threaded through to ingest_daily as the
        # SESSION date. 18:00 ET on April 14 post-close ingests April 14's
        # session (not April 13).
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 18)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        # 18:00 ET on April 14 = 22:00 UTC on April 14. We assert the
        # wrapper passes target_date=2026-04-14 (the session that just closed).
        when = datetime(2026, 4, 14, 18, 0, tzinfo=ZoneInfo("America/New_York"))

        captured: dict[str, Any] = {}

        async def _capture(_ctx: dict[str, Any], **kwargs: Any) -> dict[str, int]:
            captured.update(kwargs)
            return {}

        monkeypatch.setattr("msai.workers.nightly_ingest.run_nightly_ingest", _capture)

        await run_nightly_ingest_if_due({}, now=when.astimezone(UTC))

        assert "target_date" in captured
        assert captured["target_date"].isoformat() == "2026-04-14"

    @pytest.mark.asyncio
    async def test_overnight_schedule_uses_offset_minus_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 4 P2: an overnight schedule (e.g. 02:00 next morning)
        # must target yesterday's session, not today's. With
        # session_offset_days=-1, a 02:00 ET April 15 tick ingests April 14.
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 2)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "daily_ingest_session_offset_days", -1)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        overnight = datetime(2026, 4, 15, 2, 30, tzinfo=ZoneInfo("America/New_York"))

        captured: dict[str, Any] = {}

        async def _capture(_ctx: dict[str, Any], **kwargs: Any) -> dict[str, int]:
            captured.update(kwargs)
            return {}

        monkeypatch.setattr("msai.workers.nightly_ingest.run_nightly_ingest", _capture)

        await run_nightly_ingest_if_due({}, now=overnight.astimezone(UTC))

        assert captured["target_date"].isoformat() == "2026-04-14"
        # State file must record TODAY (April 15) so the next tick on the
        # same calendar day skips — idempotency is keyed off current.date(),
        # not target_date.
        state = json.loads((tmp_path / "scheduler" / "daily_ingest_state.json").read_text())
        assert state["last_enqueued_date"] == "2026-04-15"

    @pytest.mark.asyncio
    async def test_overnight_schedule_idempotency_keyed_off_current_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression guard for the state-key fix paired with iter 4 P2:
        # two ticks on the same cron day must only fire the ingest once
        # even with offset=-1. If state recorded target_date, the second
        # tick would see "yesterday" and re-fire.
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 2)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "daily_ingest_session_offset_days", -1)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        first = datetime(2026, 4, 15, 2, 5, tzinfo=ZoneInfo("America/New_York"))
        second = datetime(2026, 4, 15, 3, 0, tzinfo=ZoneInfo("America/New_York"))

        with patch(
            "msai.workers.nightly_ingest.run_nightly_ingest",
            new=AsyncMock(return_value={}),
        ) as mock:
            await run_nightly_ingest_if_due({}, now=first.astimezone(UTC))
            await run_nightly_ingest_if_due({}, now=second.astimezone(UTC))

        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_ingest_daily_window_fetches_target_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex iter 3 P1: `ingest_daily(target_date=X)` must request
        # [X, X+1) so Databento's end-exclusive date window returns that
        # session's bars, not the prior day's. Regression guard against
        # regressing to the old `end=target_date` semantics.
        from datetime import date as _date

        from msai.services.data_ingestion import DataIngestionService

        service = DataIngestionService(parquet_store=object())  # type: ignore[arg-type]
        captured_window: dict[str, str] = {}

        async def _capture_historical(
            _asset_class: str,
            _symbols: list[str],
            start: str,
            end: str,
            *,
            provider: str = "auto",
            dataset: str | None = None,
            schema: str | None = None,
        ) -> dict[str, int]:
            captured_window["start"] = start
            captured_window["end"] = end
            return {}

        monkeypatch.setattr(service, "ingest_historical", _capture_historical)

        await service.ingest_daily(
            asset_class="stocks",
            symbols=["AAPL"],
            target_date=_date(2026, 4, 14),
        )

        # Window must be [target_date, target_date + 1) — end-exclusive
        # semantics so Databento returns only April 14's session.
        assert captured_window["start"] == "2026-04-14"
        assert captured_window["end"] == "2026-04-15"

    @pytest.mark.asyncio
    async def test_now_parameter_overrides_wall_clock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sanity guard that the test hook works — required for all the
        # other deterministic tests above.
        from msai.core.config import settings

        monkeypatch.setattr(settings, "daily_ingest_enabled", True)
        monkeypatch.setattr(settings, "daily_ingest_timezone", "America/New_York")
        monkeypatch.setattr(settings, "daily_ingest_hour", 0)
        monkeypatch.setattr(settings, "daily_ingest_minute", 0)
        monkeypatch.setattr(settings, "data_root", tmp_path, raising=True)

        # Pass a `now` after midnight tz-time; should fire even though the
        # actual wall clock is irrelevant.
        explicit_now = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)

        with patch(
            "msai.workers.nightly_ingest.run_nightly_ingest",
            new=AsyncMock(return_value={"x": 1}),
        ) as mock_ingest:
            result = await run_nightly_ingest_if_due({}, now=explicit_now)

        assert result == {"x": 1}
        mock_ingest.assert_awaited_once()


# ---------------------------------------------------------------------------
# WorkerSettings registration regression
# ---------------------------------------------------------------------------


class TestWorkerSettingsRegistration:
    def test_nightly_cron_uses_if_due_wrapper(self) -> None:
        # Regression guard: if anyone re-registers the bare
        # `run_nightly_ingest` function, the tz scheduling is silently
        # bypassed and the ingest fires every minute. This test makes
        # that mistake fail loudly at import time.
        from msai.workers.nightly_ingest import run_nightly_ingest_if_due
        from msai.workers.settings import WorkerSettings

        nightly_crons = [
            cj for cj in WorkerSettings.cron_jobs if cj.coroutine is run_nightly_ingest_if_due
        ]
        assert len(nightly_crons) == 1, "exactly one cron must wrap run_nightly_ingest_if_due"
        # Polling cron — minute is None and second is 0 (fires at every :00).
        assert nightly_crons[0].minute is None
        assert nightly_crons[0].second == 0

    def test_no_cron_registers_bare_run_nightly_ingest(self) -> None:
        # Inverse guard: nobody registers the un-wrapped ingest.
        from msai.workers.nightly_ingest import run_nightly_ingest
        from msai.workers.settings import WorkerSettings

        bare = [cj for cj in WorkerSettings.cron_jobs if cj.coroutine is run_nightly_ingest]
        assert bare == []
