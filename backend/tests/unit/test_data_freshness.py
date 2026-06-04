"""Unit tests for the PR-1b data-freshness pure-domain layer.

Covers: GraceConfig (defaults / JSON override / fail-loud / asserts),
bar-type interval parsing, resolve_session_phase (freezegun matrix for
equities + futures incl. early-close days), FreshnessRegistry record/
snapshot + thread-safety, and evaluate() staleness matrix.

All times are anchored with freezegun so the calendar-driven phase
resolution is deterministic.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.config import ImportableActorConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.portfolio.portfolio import Portfolio

from msai.services.live.data_freshness import (
    FeedKey,
    FeedObservation,
    FreshnessRegistry,
    GraceConfig,
    SessionPhase,
    StaleFinding,
    evaluate,
    parse_interval_s,
    resolve_session_phase,
)
from msai.services.nautilus.data_freshness_actor import (
    DataFreshnessActor,
    DataFreshnessActorConfig,
)

# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

_NS = 1_000_000_000


def _ns(dt: datetime) -> int:
    """UTC datetime → epoch nanoseconds."""
    return int(dt.timestamp() * _NS)


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _feed(
    dataset: str = "EQUS.MINI",
    bar_type: str = "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
) -> FeedKey:
    return FeedKey(dataset=dataset, native_bar_type_str=bar_type)


def _phase_resolver(phase: str, phase_open_ts: int | None):
    """Build a stub phase resolver returning a fixed SessionPhase."""

    def _resolve(asset_class: str, now_utc: datetime) -> SessionPhase:
        return SessionPhase(phase=phase, phase_open_ts=phase_open_ts)

    return _resolve


# ===========================================================================
# GraceConfig
# ===========================================================================


def test_grace_config_defaults_match_spec() -> None:
    # Arrange / Act
    cfg = GraceConfig()

    # Assert
    assert cfg.grace_s("equity", "rth") == 90
    assert cfg.grace_s("equity", "pre") == 300
    assert cfg.grace_s("equity", "post") == 300
    assert cfg.grace_s("equity", "closed") is None
    assert cfg.grace_s("futures", "rth") == 120
    assert cfg.grace_s("futures", "maintenance") is None
    assert cfg.startup_grace_s == 180
    assert cfg.interval_s == 5


def test_grace_config_json_override_replaces_phase_grace() -> None:
    # Arrange
    override = '{"equity": {"rth": 45}}'

    # Act
    cfg = GraceConfig.from_env_json(override)

    # Assert
    assert cfg.grace_s("equity", "rth") == 45
    # Untouched phases keep defaults.
    assert cfg.grace_s("equity", "pre") == 300


def test_grace_config_invalid_json_raises_value_error() -> None:
    # Arrange
    bad = "{not json"

    # Act / Assert
    with pytest.raises(ValueError):
        GraceConfig.from_env_json(bad)


def test_grace_config_unknown_keys_rejected() -> None:
    # Arrange — unknown phase key under a known asset class.
    bad = '{"equity": {"lunch": 30}}'

    # Act / Assert
    with pytest.raises(ValueError):
        GraceConfig.from_env_json(bad)


def test_grace_config_unknown_asset_class_rejected() -> None:
    # Arrange
    bad = '{"crypto": {"rth": 30}}'

    # Act / Assert
    with pytest.raises(ValueError):
        GraceConfig.from_env_json(bad)


def test_grace_config_assert_interval_times_four_le_min_open_grace() -> None:
    # Arrange — interval 30 → 120; equity rth grace 90 < 120 violates the assert.
    bad = '{"equity": {"rth": 90}, "interval_s": 30}'

    # Act / Assert
    with pytest.raises(ValueError):
        GraceConfig.from_env_json(bad)


def test_grace_config_assert_startup_grace_positive() -> None:
    # Arrange
    bad = '{"startup_grace_s": 0}'

    # Act / Assert
    with pytest.raises(ValueError):
        GraceConfig.from_env_json(bad)


@pytest.mark.parametrize("bad_interval", [0, -5])
def test_grace_config_interval_s_must_be_at_least_one(bad_interval: int) -> None:
    # Arrange — interval_s drives the monitor's loop sleep; a value <= 0 would
    # busy-spin (0) or be nonsensical (negative). Fail loud at config load.
    bad = f'{{"interval_s": {bad_interval}}}'

    # Act / Assert
    with pytest.raises(ValueError):
        GraceConfig.from_env_json(bad)


def test_grace_config_startup_grace_s_zero_rejected_direct_construction() -> None:
    # Arrange / Act / Assert — the bound holds on direct construction too, not
    # only via the JSON path.
    with pytest.raises(ValueError):
        GraceConfig(startup_grace_s=0)


def test_grace_config_interval_s_zero_rejected_direct_construction() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        GraceConfig(interval_s=0)


def test_grace_config_defaults_pass_interval_and_startup_bounds() -> None:
    # Arrange / Act — the shipped defaults must satisfy the >=1 bounds.
    cfg = GraceConfig()

    # Assert
    assert cfg.interval_s >= 1
    assert cfg.startup_grace_s >= 1


# ===========================================================================
# Bar-type interval parsing
# ===========================================================================


@pytest.mark.parametrize(
    ("bar_type", "expected"),
    [
        ("AAPL.XNAS-1-MINUTE-LAST-EXTERNAL", 60),
        ("AAPL.XNAS-5-MINUTE-LAST-EXTERNAL", 300),
        ("ES.GLBX-1-HOUR-LAST-EXTERNAL", 3600),
        ("AAPL.XNAS-1-SECOND-LAST-EXTERNAL", 1),
        ("AAPL.XNAS-1-DAY-LAST-EXTERNAL", 86400),
        ("AAPL.XNAS-15-MINUTE-LAST-EXTERNAL", 900),
        # Dotted share-class symbol: the bar-spec suffix is still the LAST 4
        # dash-segments, so step/aggregation must be read from the END.
        ("BRK.B.XNYS-1-MINUTE-LAST-EXTERNAL", 60),
        ("BRK.B.XNYS-5-MINUTE-LAST-EXTERNAL", 300),
    ],
)
def test_parse_interval_s_known_aggregations(bar_type: str, expected: int) -> None:
    # Act / Assert
    assert parse_interval_s(bar_type) == expected


def test_parse_interval_s_unknown_aggregation_raises() -> None:
    # Act / Assert — TICK is not a time aggregation.
    with pytest.raises(ValueError):
        parse_interval_s("AAPL.XNAS-100-TICK-LAST-EXTERNAL")


def test_parse_interval_s_malformed_string_raises() -> None:
    with pytest.raises(ValueError):
        parse_interval_s("garbage")


def test_parse_interval_s_too_few_segments_raises() -> None:
    # Fewer than 5 dash-segments → cannot locate the STEP-AGG-PRICE-SOURCE
    # suffix; fail-loud.
    with pytest.raises(ValueError):
        parse_interval_s("AAPL.XNAS-1-MINUTE-LAST")


# ===========================================================================
# FeedKey display symbol
# ===========================================================================


def test_feed_key_symbol_strips_venue() -> None:
    # Arrange
    fk = _feed(bar_type="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")

    # Act / Assert
    assert fk.symbol == "AAPL"
    assert fk.expected_interval_s == 60


def test_feed_key_symbol_futures() -> None:
    fk = _feed(dataset="GLBX.MDP3", bar_type="ESM6.GLBX-1-MINUTE-LAST-EXTERNAL")
    assert fk.symbol == "ESM6"
    assert fk.expected_interval_s == 60


def test_feed_key_symbol_dotted_share_class() -> None:
    # ``BRK.B.XNYS-...`` must resolve to symbol ``BRK.B`` (venue ``XNYS``
    # stripped from the RIGHT), NOT ``BRK`` (first-dot bug).
    fk = _feed(dataset="EQUS.MINI", bar_type="BRK.B.XNYS-1-MINUTE-LAST-EXTERNAL")
    assert fk.symbol == "BRK.B"
    assert fk.expected_interval_s == 60


# ===========================================================================
# resolve_session_phase — equities
# ===========================================================================

# 2026-11-25 (Wed) is a regular XNYS session: open 14:30 UTC, close 21:00 UTC.
# pre  = [09:00, 14:30) UTC
# post = [21:00, 01:00 next-day) UTC


@pytest.mark.parametrize(
    ("now", "expected_phase"),
    [
        (_utc(2026, 11, 25, 8, 0), "closed"),  # before pre window
        (_utc(2026, 11, 25, 10, 0), "pre"),  # in pre-market
        (_utc(2026, 11, 25, 14, 30), "rth"),  # exactly open
        (_utc(2026, 11, 25, 18, 0), "rth"),  # mid-session
        (_utc(2026, 11, 25, 21, 0), "post"),  # exactly close → post
        (_utc(2026, 11, 25, 23, 0), "post"),  # in post-market
    ],
)
def test_resolve_session_phase_equity_regular_day(now: datetime, expected_phase: str) -> None:
    # Act
    with freeze_time(now):
        sp = resolve_session_phase("equity", now)

    # Assert
    assert sp.phase == expected_phase


def test_resolve_session_phase_equity_early_close_shifts_post() -> None:
    # Arrange — 2026-11-27 day-after-Thanksgiving is an early close
    # (close 18:00 UTC = 13:00 ET). At 18:30 UTC (13:30 ET) we are in POST,
    # NOT rth and NOT closed — proving post anchors on the actual session close.
    now = _utc(2026, 11, 27, 18, 30)

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("equity", now)

    # Assert
    assert sp.phase == "post"


def test_resolve_session_phase_equity_early_close_pre_close_is_rth() -> None:
    # Arrange — 17:00 UTC (12:00 ET) on the early-close day is still RTH.
    now = _utc(2026, 11, 27, 17, 0)

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("equity", now)

    # Assert
    assert sp.phase == "rth"


def test_resolve_session_phase_equity_weekend_is_closed() -> None:
    # Arrange — Saturday.
    now = _utc(2026, 11, 28, 18, 0)

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("equity", now)

    # Assert
    assert sp.phase == "closed"
    assert sp.phase_open_ts is None


def test_resolve_session_phase_equity_rth_returns_phase_open_ts() -> None:
    # Arrange
    now = _utc(2026, 11, 25, 18, 0)

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("equity", now)

    # Assert — phase_open_ts is the session open (14:30 UTC) in ns.
    assert sp.phase == "rth"
    assert sp.phase_open_ts == _ns(_utc(2026, 11, 25, 14, 30))


# ===========================================================================
# resolve_session_phase — futures
# ===========================================================================

# CMES Wed 2026-11-25 session: open Tue 23:00 UTC (17:00 CT),
# close Wed 23:00 UTC. Maintenance window is 16:00-17:00 CT (22:00-23:00 UTC).


def test_resolve_session_phase_futures_rth_midday() -> None:
    # Arrange — 18:00 UTC Wed = midday CT, an open trading phase.
    now = _utc(2026, 11, 25, 18, 0)

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("futures", now)

    # Assert
    assert sp.phase == "rth"
    assert sp.phase_open_ts is not None


def test_resolve_session_phase_futures_maintenance_window() -> None:
    # Arrange — 22:30 UTC = 16:30 CT, inside the 16:00-17:00 CT maintenance gap.
    now = _utc(2026, 11, 25, 22, 30)

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("futures", now)

    # Assert
    assert sp.phase == "maintenance"


def test_resolve_session_phase_futures_weekend_is_closed() -> None:
    # Arrange — Saturday, CMES closed (no session label contains Saturday).
    now = _utc(2026, 11, 28, 18, 0)

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("futures", now)

    # Assert
    assert sp.phase == "closed"


def test_resolve_session_phase_futures_sunday_evening_reopen_is_rth() -> None:
    # Arrange — Sunday-evening Globex reopen. The Monday-labeled session
    # opens Sunday 17:00 CT, so 17:30 CT Sunday is INSIDE an open session.
    # (Sessions are close-date-anchored in exchange_calendars; the resolver
    # must find the session label whose [open, close) window contains now.)
    now = _utc(2026, 11, 29, 23, 30)  # Sunday 17:30 CT

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("futures", now)

    # Assert — the reopen is a live trading phase, not closed.
    assert sp.phase == "rth"
    assert sp.phase_open_ts is not None
    # phase_open_ts must be the CONTAINING (Monday-labeled) session's open
    # = Sunday 17:00 CT = 23:00 UTC, so the phase-open clamp works across
    # the evening.
    assert sp.phase_open_ts == _ns(_utc(2026, 11, 29, 23, 0))


def test_resolve_session_phase_futures_monday_evening_reopen_is_rth() -> None:
    # Arrange — Monday 17:30 CT. Monday's session closed at 17:00 CT, but the
    # Tuesday-labeled session opened Monday 17:00 CT, so 17:30 CT Monday is
    # inside the Tuesday-labeled session.  (Normal, non-holiday week.)
    now = _utc(2026, 6, 8, 22, 30)  # Monday 17:30 CT

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("futures", now)

    # Assert
    assert sp.phase == "rth"
    assert sp.phase_open_ts is not None
    # Containing session opened Monday 17:00 CT = 22:00 UTC.
    assert sp.phase_open_ts == _ns(_utc(2026, 6, 8, 22, 0))


def test_resolve_session_phase_futures_weekday_evening_maintenance() -> None:
    # Arrange — Monday 16:30 CT, inside the 16:00-17:00 CT maintenance gap of
    # Monday's session (open Sunday 17:00 → close Monday 17:00 CT).
    now = _utc(2026, 6, 8, 21, 30)  # Monday 16:30 CT

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("futures", now)

    # Assert
    assert sp.phase == "maintenance"


def test_resolve_session_phase_futures_friday_evening_is_closed() -> None:
    # Arrange — Friday 17:30 CT. Friday's session closed at 17:00 CT and the
    # next session is Monday (no Saturday session opens Friday evening), so no
    # session label contains 17:30 CT Friday → closed.
    now = _utc(2026, 6, 12, 22, 30)  # Friday 17:30 CT

    # Act
    with freeze_time(now):
        sp = resolve_session_phase("futures", now)

    # Assert
    assert sp.phase == "closed"


# ===========================================================================
# FreshnessRegistry
# ===========================================================================


def test_registry_record_and_snapshot_round_trip() -> None:
    # Arrange
    reg = FreshnessRegistry()
    fk = _feed()
    event_ns = _ns(_utc(2026, 11, 25, 18, 0))
    arrival_ns = event_ns + 5 * _NS

    # Act
    reg.record(fk, event_ns, arrival_ns)
    snap = reg.snapshot()

    # Assert
    assert snap[fk] == FeedObservation(ts_event_ns=event_ns, ts_arrival_ns=arrival_ns)


def test_registry_record_keeps_latest_event_ts() -> None:
    # Arrange
    reg = FreshnessRegistry()
    fk = _feed()
    first = _ns(_utc(2026, 11, 25, 18, 0))

    # Act — record older then newer; snapshot must reflect newest.
    reg.record(fk, first, first)
    reg.record(fk, first + 60 * _NS, first + 60 * _NS)
    snap = reg.snapshot()

    # Assert
    assert snap[fk].ts_event_ns == first + 60 * _NS


def test_registry_thread_safety_smoke() -> None:
    # Arrange
    reg = FreshnessRegistry()
    base = _ns(_utc(2026, 11, 25, 18, 0))
    feeds = [_feed(bar_type=f"SYM{i}.XNAS-1-MINUTE-LAST-EXTERNAL") for i in range(20)]

    def _worker(start: int) -> None:
        for i in range(start, start + 10):
            reg.record(feeds[i % 20], base + i * _NS, base + i * _NS)

    # Act — two threads recording concurrently.
    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(10,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Assert — no lost keys, no corruption.
    snap = reg.snapshot()
    assert len(snap) == 20


def test_registry_dataset_max_event_ts_helper() -> None:
    # Arrange — two feeds in the same dataset.
    reg = FreshnessRegistry()
    fk_a = _feed(bar_type="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
    fk_b = _feed(bar_type="MSFT.XNAS-1-MINUTE-LAST-EXTERNAL")
    older = _ns(_utc(2026, 11, 25, 18, 0))
    newer = older + 30 * _NS
    reg.record(fk_a, older, older)
    reg.record(fk_b, newer, newer)

    # Act
    snap = reg.snapshot()

    # Assert — both present; the max helper is derived in evaluate, so we
    # assert the raw data the helper consumes.
    assert max(o.ts_event_ns for o in snap.values()) == newer


# ===========================================================================
# evaluate()
# ===========================================================================


def _open_resolver(phase_open: datetime):
    return _phase_resolver("rth", _ns(phase_open))


def test_evaluate_warm_feed_no_finding() -> None:
    # Arrange — feed updated 30s ago, interval 60s + grace 90s → budget 150s.
    cfg = GraceConfig()
    fk = _feed()
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)
    snap = {fk: FeedObservation(ts_event_ns=now_ns - 30 * _NS, ts_arrival_ns=now_ns)}

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert
    assert findings == []


def test_evaluate_feed_stale_past_interval_plus_grace() -> None:
    # Arrange — silent 200s > 60 + 90 = 150 budget.
    cfg = GraceConfig()
    fk = _feed()
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)
    snap = {fk: FeedObservation(ts_event_ns=now_ns - 200 * _NS, ts_arrival_ns=now_ns)}

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert — at least one feed-granularity finding for this feed.
    feed_findings = [f for f in findings if f.granularity == "feed"]
    assert any(f.feed_key == fk for f in feed_findings)
    assert all(isinstance(f, StaleFinding) for f in findings)


def test_evaluate_5min_feed_at_4min_no_finding() -> None:
    # Arrange — 5-MINUTE feed silent 240s. Budget = 300 (interval) + 90 grace
    # = 390s. 240 < 390 → NO finding (interval parsed, not hardcoded 60s).
    cfg = GraceConfig()
    fk = _feed(bar_type="AAPL.XNAS-5-MINUTE-LAST-EXTERNAL")
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)
    snap = {fk: FeedObservation(ts_event_ns=now_ns - 240 * _NS, ts_arrival_ns=now_ns)}

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert
    assert findings == []


def test_evaluate_whole_dataset_silent_dataset_finding() -> None:
    # Arrange — two feeds in one dataset, both silent past dataset budget.
    cfg = GraceConfig()
    fk_a = _feed(bar_type="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
    fk_b = _feed(bar_type="MSFT.XNAS-1-MINUTE-LAST-EXTERNAL")
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)
    snap = {
        fk_a: FeedObservation(ts_event_ns=now_ns - 500 * _NS, ts_arrival_ns=now_ns),
        fk_b: FeedObservation(ts_event_ns=now_ns - 500 * _NS, ts_arrival_ns=now_ns),
    }

    # Act
    findings = evaluate(
        required_feeds=[fk_a, fk_b],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert — a dataset-granularity finding exists.
    assert any(f.granularity == "dataset" and f.dataset == "EQUS.MINI" for f in findings)


def test_evaluate_dataset_finding_reports_real_last_event_not_clamp() -> None:
    # Arrange — dataset goes stale right after session open while the only
    # real events are OLDER than the open. The phase-open clamp inflates the
    # comparison ts to phase_open, but the finding's last_event_ts must be the
    # honest max-observed real event ts, not the clamped value.
    cfg = GraceConfig()
    fk_a = _feed(bar_type="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
    fk_b = _feed(bar_type="MSFT.XNAS-1-MINUTE-LAST-EXTERNAL")
    phase_open = _utc(2026, 11, 25, 18, 0)
    # now is 1000s past the open → past the dataset budget even with the clamp.
    now = _utc(2026, 11, 25, 18, 16, 40)
    now_ns = _ns(now)
    phase_open_ns = _ns(phase_open)
    # Real events predate the open by an hour and two hours respectively.
    real_a_ns = phase_open_ns - 3600 * _NS
    real_b_ns = phase_open_ns - 7200 * _NS
    snap = {
        fk_a: FeedObservation(ts_event_ns=real_a_ns, ts_arrival_ns=now_ns),
        fk_b: FeedObservation(ts_event_ns=real_b_ns, ts_arrival_ns=now_ns),
    }

    # Act
    findings = evaluate(
        required_feeds=[fk_a, fk_b],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert — the dataset finding's last_event_ts is the real most-recent
    # event (real_a_ns), NOT the clamp (phase_open_ns).
    dataset_findings = [f for f in findings if f.granularity == "dataset"]
    assert len(dataset_findings) == 1
    assert dataset_findings[0].last_event_ts == real_a_ns
    assert dataset_findings[0].last_event_ts != phase_open_ns


def test_evaluate_closed_phase_never_stale() -> None:
    # Arrange — even with an ancient event ts, closed phase → no finding.
    cfg = GraceConfig()
    fk = _feed()
    now = _utc(2026, 11, 28, 18, 0)
    now_ns = _ns(now)
    snap = {fk: FeedObservation(ts_event_ns=now_ns - 999_999 * _NS, ts_arrival_ns=now_ns)}

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_phase_resolver("closed", None),
    )

    # Assert
    assert findings == []


def test_evaluate_maintenance_phase_never_stale() -> None:
    # Arrange
    cfg = GraceConfig()
    fk = _feed(dataset="GLBX.MDP3", bar_type="ESM6.GLBX-1-MINUTE-LAST-EXTERNAL")
    now = _utc(2026, 11, 25, 22, 30)
    now_ns = _ns(now)
    snap = {fk: FeedObservation(ts_event_ns=now_ns - 999_999 * _NS, ts_arrival_ns=now_ns)}

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_phase_resolver("maintenance", None),
    )

    # Assert
    assert findings == []


def test_evaluate_absent_feed_within_startup_grace_no_finding() -> None:
    # Arrange — required feed never observed; monitor started 60s ago,
    # startup grace 180s → within grace → no finding.
    cfg = GraceConfig()
    fk = _feed()
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=now_ns - 60 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert
    assert findings == []


def test_evaluate_absent_feed_past_startup_grace_finding() -> None:
    # Arrange — required feed never observed; monitor started 300s ago > 180s.
    cfg = GraceConfig()
    fk = _feed()
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=now_ns - 300 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert — absent feed (manifest-minus-snapshot) auto-halts.
    assert any(f.feed_key == fk and f.last_event_ts is None for f in findings)


def test_evaluate_absent_5min_feed_within_cadence_budget_no_finding() -> None:
    # FIX 1(b) — a 5-MINUTE absent feed must NOT false-halt at the fixed 180s
    # startup grace: its first bar can legitimately arrive ~300s+ after monitor
    # start. The absent-feed budget per feed is now
    # max(startup_grace_s, expected_interval_s + phase_grace) =
    # max(180, 300 + 90) = 390s. Monitor started 200s ago < 390 → NO finding.
    cfg = GraceConfig()
    fk = _feed(bar_type="AAPL.XNAS-5-MINUTE-LAST-EXTERNAL")
    assert fk.expected_interval_s == 300
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=now_ns - 200 * _NS,  # 200s < 390 budget
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert — no false halt during the legitimate 5-min first-bar window.
    assert findings == []


def test_evaluate_absent_5min_feed_past_cadence_budget_finding() -> None:
    # FIX 1(b) — once the 5-min cadence budget (max(180, 300+90)=390s) is
    # exceeded, the absent feed IS a finding (the feed genuinely never arrived).
    cfg = GraceConfig()
    fk = _feed(bar_type="AAPL.XNAS-5-MINUTE-LAST-EXTERNAL")
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)

    # Act — 400s > 390 budget.
    findings = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=now_ns - 400 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert
    assert any(f.feed_key == fk and f.last_event_ts is None for f in findings)


def test_evaluate_absent_1min_feed_keeps_180s_budget() -> None:
    # FIX 1(b) — a 1-MINUTE feed keeps the prior 180s behavior:
    # max(180, 60 + 90) = 180. Monitor started 200s ago > 180 → finding;
    # this pins that the cadence change does NOT relax the fast-feed budget.
    cfg = GraceConfig()
    fk = _feed()  # 1-MINUTE
    assert fk.expected_interval_s == 60
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)

    # 200s > max(180, 60+90)=180 → finding.
    findings = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=now_ns - 200 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )
    assert any(f.feed_key == fk and f.last_event_ts is None for f in findings)

    # And 100s < 180 budget → NO finding (the 180s floor still holds).
    findings_within = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=now_ns - 100 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )
    assert findings_within == []


def test_evaluate_absent_feed_pre_open_startup_no_false_halt_at_open() -> None:
    # FIX 1(c) — the absent-feed budget must be measured from the PHASE OPEN, not
    # from monitor_started alone. A node started 3h BEFORE RTH open (normal
    # pre-RTH startup) accumulates hours of wall-clock while the phase is closed
    # (closed phases skip evaluation), so on the FIRST open tick
    # ``now - monitor_started_ns`` is already enormous. Without the phase-open
    # clamp an absent feed false-halts on that first open tick. With the clamp
    # elapsed = ``now - max(monitor_started, phase_open)`` = 30s < 180 budget →
    # NO finding.
    cfg = GraceConfig()
    fk = _feed()  # 1-MINUTE → budget max(180, 60+90) = 180
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 14, 30, 30)  # open + 30s
    now_ns = _ns(now)

    # Act — monitor started 3h before the phase opened.
    findings = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=_ns(phase_open) - 3 * 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert — the pre-open startup time is clamped away; no false halt.
    assert findings == []


def test_evaluate_absent_feed_pre_open_startup_finding_after_clamped_budget() -> None:
    # FIX 1(c) — detection still fires: at phase_open + budget + ε the clamped
    # elapsed exceeds the absent budget even though monitor_started is far
    # earlier. Budget for the 1-MINUTE feed = max(180, 60+90) = 180s.
    cfg = GraceConfig()
    fk = _feed()  # 1-MINUTE
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 14, 33, 1)  # open + 180s + 1s
    now_ns = _ns(now)

    # Act — monitor started 3h before the phase opened.
    findings = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=_ns(phase_open) - 3 * 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert — past the clamped budget, the absent feed is a finding.
    assert any(f.feed_key == fk and f.last_event_ts is None for f in findings)


def test_evaluate_absent_feed_mid_session_start_clamp_keeps_monitor_started() -> None:
    # FIX 1(c) — the max() clamp must NOT loosen the mid-session case. When the
    # monitor started AFTER phase open (a node spun up mid-RTH),
    # max(monitor_started, phase_open) = monitor_started, so the budget is still
    # measured from monitor_started — detection fires at the same wall-clock time
    # it did before the clamp existed. Monitor started 200s ago > 180 budget.
    cfg = GraceConfig()
    fk = _feed()  # 1-MINUTE → budget max(180, 60+90) = 180
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)  # well into RTH, hours after open
    now_ns = _ns(now)
    monitor_started_ns = now_ns - 200 * _NS  # mid-session, after phase_open
    assert monitor_started_ns > _ns(phase_open)

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot={},
        now_ns=now_ns,
        monitor_started_ns=monitor_started_ns,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert — clamp keeps monitor_started authoritative; finding still fires.
    assert any(f.feed_key == fk and f.last_event_ts is None for f in findings)


def test_evaluate_session_reopen_no_halt_until_phase_open_plus_budget() -> None:
    # Arrange — yesterday's last event is ancient, but the phase just opened
    # 30s ago. With the phase-open clamp the budget restarts at phase_open,
    # so 30s < 60 + 90 = 150 budget → NO finding (staleness not carried
    # across the boundary).
    cfg = GraceConfig()
    fk = _feed()
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 14, 30, 30)  # 30s after open
    now_ns = _ns(now)
    yesterday = _ns(_utc(2026, 11, 24, 21, 0))  # prior close
    snap = {fk: FeedObservation(ts_event_ns=yesterday, ts_arrival_ns=yesterday)}

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert
    assert findings == []


def test_evaluate_stale_timestamp_mode_event_frozen_finding() -> None:
    # Arrange — arrivals are fresh (now) but event ts is frozen 300s ago.
    # Staleness is measured on EVENT ts, so this is stale: 300 > 150 budget.
    cfg = GraceConfig()
    fk = _feed()
    phase_open = _utc(2026, 11, 25, 14, 30)
    now = _utc(2026, 11, 25, 18, 0)
    now_ns = _ns(now)
    snap = {
        fk: FeedObservation(
            ts_event_ns=now_ns - 300 * _NS,  # frozen event clock
            ts_arrival_ns=now_ns,  # arrivals still flowing
        )
    }

    # Act
    findings = evaluate(
        required_feeds=[fk],
        snapshot=snap,
        now_ns=now_ns,
        monitor_started_ns=now_ns - 3600 * _NS,
        cfg=cfg,
        phase_resolver=_open_resolver(phase_open),
    )

    # Assert
    assert any(f.feed_key == fk for f in findings if f.granularity == "feed")


# ===========================================================================
# DataFreshnessActor + live_node_config wiring (PR 1b Task 2)
# ===========================================================================
#
# These tests import nautilus_trader (fine in the test env — the uvloop
# policy gotcha only bites arq workers). They cover the actor's config
# round-trip, on_bar recording, the safe no-op when no registry is
# injected, and the live_node_config builder appending the freshness
# actor's ImportableActorConfig with correctly-derived bar_type_datasets.
# Nautilus + actor imports live at the top of the file (E402) alongside
# the pure-domain imports.

_FRESH_NATIVE_BAR_TYPES: list[str] = ["AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"]
_FRESH_BAR_TYPE_DATASETS: dict[str, str] = {
    "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI",
}


def _register_actor_with_bus(actor: DataFreshnessActor, bus: MessageBus) -> None:
    """Register the actor against a real MessageBus + minimal Nautilus deps.

    ``Actor.clock`` / ``Actor.msgbus`` are Cython read-only properties, so the
    only way to inject them is via ``register_base(portfolio, msgbus, cache,
    clock)`` — same pattern as the SymbologyShimActor test helper.
    """
    clock = LiveClock()
    cache = Cache()
    portfolio = Portfolio(msgbus=bus, cache=cache, clock=clock)
    actor.register_base(portfolio=portfolio, msgbus=bus, cache=cache, clock=clock)


def _build_native_bar(
    bar_type_str: str = "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
    ts_event: int = 1_700_000_000_000_000_000,
) -> Bar:
    bar_type = BarType.from_str(bar_type_str)
    return Bar(
        bar_type,
        Price.from_str("200.00"),
        Price.from_str("201.00"),
        Price.from_str("199.00"),
        Price.from_str("200.50"),
        Quantity.from_str("1000"),
        ts_event,
        ts_event + 1,
    )


class _StubRegistry:
    """Captures record() calls so tests can assert FeedKey + timestamps."""

    def __init__(self) -> None:
        self.calls: list[tuple[FeedKey, int, int]] = []

    def record(self, feed_key: FeedKey, ts_event_ns: int, ts_arrival_ns: int) -> None:
        self.calls.append((feed_key, ts_event_ns, ts_arrival_ns))


def _build_fresh_actor() -> DataFreshnessActor:
    return DataFreshnessActor(
        config=DataFreshnessActorConfig(
            native_bar_types=list(_FRESH_NATIVE_BAR_TYPES),
            bar_type_datasets=dict(_FRESH_BAR_TYPE_DATASETS),
        ),
    )


# --- Config round-trip through ImportableActorConfig (primitives only) ----


def test_data_freshness_config_roundtrips_through_importable_actor_config() -> None:
    """The config must carry primitives only and survive the kernel's
    ImportableActorConfig → ActorFactory.create reconstruction path."""
    from nautilus_trader.common.config import ActorFactory

    importable = ImportableActorConfig(
        actor_path="msai.services.nautilus.data_freshness_actor:DataFreshnessActor",
        config_path="msai.services.nautilus.data_freshness_actor:DataFreshnessActorConfig",
        config={
            "native_bar_types": list(_FRESH_NATIVE_BAR_TYPES),
            "bar_type_datasets": dict(_FRESH_BAR_TYPE_DATASETS),
        },
    )

    # Act — the kernel uses ActorFactory.create to instantiate from the
    # serialized importable config; this is the real reconstruction path.
    actor = ActorFactory.create(importable)

    # Assert — round-tripped primitives match what we put in.
    assert isinstance(actor, DataFreshnessActor)
    assert actor._fresh_config.native_bar_types == _FRESH_NATIVE_BAR_TYPES
    assert actor._fresh_config.bar_type_datasets == _FRESH_BAR_TYPE_DATASETS


# --- on_bar records into an injected registry with the right FeedKey ------


def test_on_bar_records_event_and_arrival_with_correct_feed_key() -> None:
    """on_bar records bar.ts_event as the event time and the actor clock's
    ns as the arrival time, keyed by FeedKey(dataset, native_bar_type_str)."""
    actor = _build_fresh_actor()
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)

    stub = _StubRegistry()
    actor.set_registry(stub)  # type: ignore[arg-type]  # duck-typed registry stub

    # on_start precomputes the BarType -> FeedKey map that on_bar reads.
    actor.on_start()

    bar = _build_native_bar(ts_event=1_700_000_000_000_000_000)
    actor.on_bar(bar)

    assert len(stub.calls) == 1
    feed_key, ts_event_ns, ts_arrival_ns = stub.calls[0]
    assert feed_key == FeedKey(
        dataset="EQUS.MINI",
        native_bar_type_str="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
    )
    assert ts_event_ns == 1_700_000_000_000_000_000
    # Arrival comes from the actor clock — a real (large) ns timestamp,
    # distinct from the frozen event ts.
    assert ts_arrival_ns > 0


# --- Registry-injection: actor records internally before/without injection --


def test_on_bar_records_internally_before_set_registry() -> None:
    """FIX 1(a) — the actor ALWAYS records into its OWN internal registry from
    the first bar, even before ``set_registry`` is ever called. Bars delivered
    between node start and the post-build registry injection must NOT be lost."""
    actor = _build_fresh_actor()
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)
    # NB: set_registry deliberately NOT called yet.

    actor.on_start()
    bar = _build_native_bar(ts_event=1_700_000_000_000_000_000)
    actor.on_bar(bar)  # must not raise; recorded internally

    # The actor's own registry holds the observation keyed by the right FeedKey.
    fk = FeedKey(dataset="EQUS.MINI", native_bar_type_str="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
    snap = actor._registry.snapshot()  # noqa: SLF001
    assert fk in snap
    assert snap[fk].ts_event_ns == 1_700_000_000_000_000_000


def test_set_registry_replays_internal_snapshot_into_shared() -> None:
    """FIX 1(a) — pre-injection bars recorded into the internal registry are
    REPLAYED into the shared registry on ``set_registry`` (no observation lost),
    and subsequent bars record into the shared registry too."""
    actor = _build_fresh_actor()
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)
    actor.on_start()

    fk = FeedKey(dataset="EQUS.MINI", native_bar_type_str="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")

    # Pre-injection bar.
    early = _build_native_bar(ts_event=1_700_000_000_000_000_000)
    actor.on_bar(early)

    # Inject the shared registry AFTER the early bar (the real ordering).
    shared = FreshnessRegistry()
    actor.set_registry(shared)

    # Replay landed the early observation in the SHARED registry.
    shared_snap = shared.snapshot()
    assert fk in shared_snap
    assert shared_snap[fk].ts_event_ns == 1_700_000_000_000_000_000

    # A later bar also records into the shared registry (newest event wins).
    late = _build_native_bar(ts_event=1_700_000_000_000_000_000 + 60 * _NS)
    actor.on_bar(late)
    assert shared.snapshot()[fk].ts_event_ns == 1_700_000_000_000_000_000 + 60 * _NS


def test_set_registry_swaps_before_replaying() -> None:
    """FIX 3 — ``set_registry`` must SWAP ``self._registry`` to the shared
    registry FIRST, then replay the old internal snapshot into it. If the swap
    happened AFTER the replay, a bar recorded during the handoff (e.g. a bar
    delivered while ``record`` is mid-replay) would land in the soon-discarded
    internal registry and be lost. We assert the swap-first ordering by spying
    on the shared registry's ``record``: at the moment replay calls it, the
    actor must ALREADY point at the shared registry."""
    actor = _build_fresh_actor()
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)
    actor.on_start()

    fk = FeedKey(dataset="EQUS.MINI", native_bar_type_str="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")

    # One pre-injection observation so replay calls ``shared.record`` at least
    # once (the moment we inspect the actor's pointer).
    early = _build_native_bar(ts_event=1_700_000_000_000_000_000)
    actor.on_bar(early)

    observed_pointer_is_shared: list[bool] = []

    class _SwapSpyRegistry(FreshnessRegistry):
        def record(self, feed_key: FeedKey, ts_event_ns: int, ts_arrival_ns: int) -> None:
            # When replay drives this during set_registry, the actor must
            # already have swapped its pointer to THIS registry (swap-first).
            observed_pointer_is_shared.append(actor._registry is self)  # noqa: SLF001
            super().record(feed_key, ts_event_ns, ts_arrival_ns)

    shared = _SwapSpyRegistry()
    actor.set_registry(shared)

    # Replay invoked shared.record, and at that point the pointer was already
    # the shared registry (swap-first ordering).
    assert observed_pointer_is_shared == [True]
    # And the replayed observation actually landed in the shared registry.
    assert shared.snapshot()[fk].ts_event_ns == 1_700_000_000_000_000_000
    # Post-condition: the actor now points at the shared registry.
    assert actor._registry is shared  # noqa: SLF001


def test_set_registry_and_on_bar_are_mutually_exclusive_no_observation_lost() -> None:
    """FIX 1 (iter 8) — the ENTIRE ``set_registry`` handoff (swap → snapshot →
    replay) is mutually exclusive with ``on_bar``'s record via the actor-level
    ``_handoff_lock``. This DETERMINISTICALLY proves the race is closed
    completely (not merely narrowed): a bar that begins recording during the
    handoff is fully serialized AFTER the handoff and lands in the shared
    registry — it can never be stranded in the snapshotted-then-discarded
    internal registry.

    Construction:
      * The actor's OLD internal registry is a stub whose ``snapshot()`` blocks
        on an event, so ``set_registry`` parks INSIDE its critical section
        while holding ``_handoff_lock``.
      * Thread A enters ``set_registry`` → acquires the lock → swaps → calls
        ``old.snapshot()`` which signals it entered and blocks.
      * Thread B then calls ``on_bar``; it MUST block on ``_handoff_lock``
        (held by A) and therefore record nothing yet.
      * We assert B is blocked (shared registry still empty), then release the
        snapshot; A completes + drops the lock; B records into the shared
        registry. The in-flight observation is present post-handoff.
    """
    actor = _build_fresh_actor()
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)
    actor.on_start()

    fk = FeedKey(dataset="EQUS.MINI", native_bar_type_str="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")

    snapshot_entered = threading.Event()
    replay_may_proceed = threading.Event()

    class _BlockingSnapshotRegistry(FreshnessRegistry):
        """Internal (OLD) registry whose snapshot() parks set_registry inside
        its critical section so we can interleave a concurrent on_bar."""

        def snapshot(self) -> dict[FeedKey, FeedObservation]:
            snapshot_entered.set()
            # Block until the test lets the handoff finish — set_registry holds
            # ``_handoff_lock`` for the duration of this call.
            assert replay_may_proceed.wait(timeout=5.0)
            return super().snapshot()

    # Install the blocking internal registry as the actor's current registry.
    actor._registry = _BlockingSnapshotRegistry()  # noqa: SLF001

    shared = FreshnessRegistry()

    set_registry_error: list[BaseException] = []
    on_bar_error: list[BaseException] = []

    def _do_set_registry() -> None:
        try:
            actor.set_registry(shared)
        except BaseException as exc:  # noqa: BLE001 — surface to the test thread
            set_registry_error.append(exc)

    # The in-flight bar that begins recording during the handoff.
    in_flight_event_ts = 1_700_000_000_000_000_000 + 120 * _NS
    in_flight_bar = _build_native_bar(ts_event=in_flight_event_ts)

    def _do_on_bar() -> None:
        try:
            actor.on_bar(in_flight_bar)
        except BaseException as exc:  # noqa: BLE001 — surface to the test thread
            on_bar_error.append(exc)

    thread_a = threading.Thread(target=_do_set_registry, name="set_registry")
    thread_a.start()

    # Wait until set_registry is parked inside snapshot() holding the lock.
    assert snapshot_entered.wait(timeout=5.0)

    thread_b = threading.Thread(target=_do_on_bar, name="on_bar")
    thread_b.start()

    # on_bar must be BLOCKED on _handoff_lock — give it a moment to try, then
    # assert it has recorded nothing in the shared registry yet.
    thread_b.join(timeout=0.2)
    assert thread_b.is_alive(), "on_bar should be blocked on the handoff lock"
    assert fk not in shared.snapshot(), "on_bar recorded during the locked handoff"

    # Let the handoff finish; set_registry drops the lock, on_bar proceeds.
    replay_may_proceed.set()
    thread_a.join(timeout=5.0)
    thread_b.join(timeout=5.0)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert not set_registry_error, f"set_registry raised: {set_registry_error}"
    assert not on_bar_error, f"on_bar raised: {on_bar_error}"

    # The in-flight observation landed in the SHARED registry (never lost), and
    # the actor now points at the shared registry.
    final = shared.snapshot()
    assert fk in final
    assert final[fk].ts_event_ns == in_flight_event_ts
    assert actor._registry is shared  # noqa: SLF001


def test_on_bar_never_crashes_when_set_registry_never_called() -> None:
    """FIX 1(a) — the actor must still never crash if ``set_registry`` is never
    called; it just records internally across many bars."""
    actor = _build_fresh_actor()
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)
    actor.on_start()

    bar = _build_native_bar()
    # Many bars, no shared registry ever injected — no exception.
    for _ in range(5):
        actor.on_bar(bar)

    # The internal registry holds exactly one (deduped) observation.
    fk = FeedKey(dataset="EQUS.MINI", native_bar_type_str="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
    assert fk in actor._registry.snapshot()  # noqa: SLF001


# --- live_node_config: freshness actor appended with derived datasets -----


def test_build_per_account_config_appends_freshness_actor_with_datasets() -> None:
    """build_per_account_trading_node_config must append the
    DataFreshnessActor's ImportableActorConfig (after the shim) with
    bar_type_datasets derived from canonical_to_native + venue_dataset_map."""
    from msai.services.nautilus.live_node_config import (
        build_per_account_trading_node_config,
    )

    config = build_per_account_trading_node_config(
        account_id="DUP733214",
        ibg_client_id=210,
        ib_login_key="marin1016test",
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        databento_api_key="dbn-test-key",
        ib_host="ib-gateway",
        ib_port=4002,
    )

    fresh_entry = next(
        (a for a in config.actors if "DataFreshnessActor" in a.actor_path),
        None,
    )
    assert fresh_entry is not None, (
        f"DataFreshnessActor missing; got actor_paths={[a.actor_path for a in config.actors]!r}"
    )
    assert (
        fresh_entry.actor_path == "msai.services.nautilus.data_freshness_actor:DataFreshnessActor"
    )
    assert (
        fresh_entry.config_path
        == "msai.services.nautilus.data_freshness_actor:DataFreshnessActorConfig"
    )
    fresh_cfg = fresh_entry.config
    assert fresh_cfg["native_bar_types"] == ["AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"]
    assert fresh_cfg["bar_type_datasets"] == {
        "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI",
    }
    # Ordering: freshness actor lands AFTER the shim.
    shim_idx = next(i for i, a in enumerate(config.actors) if "SymbologyShimActor" in a.actor_path)
    fresh_idx = next(i for i, a in enumerate(config.actors) if "DataFreshnessActor" in a.actor_path)
    assert fresh_idx > shim_idx


def test_build_per_account_config_dotted_symbol_maps_to_dataset() -> None:
    """A dotted share-class native bar type (``BRK.B.XNYS-...``) must derive
    its venue authoritatively (``XNYS``) so bar_type_datasets includes the feed
    with the right dataset. The first-dot bug yielded venue ``B.XNYS``, which
    missed venue_dataset_map and SILENTLY dropped the feed from the manifest."""
    from msai.services.nautilus.live_node_config import (
        build_per_account_trading_node_config,
    )

    config = build_per_account_trading_node_config(
        account_id="DUP733214",
        ibg_client_id=210,
        ib_login_key="marin1016test",
        native_instrument_ids=["BRK.B.XNYS"],
        venue_dataset_map={"XNYS": "EQUS.MINI"},
        canonical_to_native_bar_types={
            "BRK.B.IBKR-1-MINUTE-LAST-EXTERNAL": "BRK.B.XNYS-1-MINUTE-LAST-EXTERNAL",
        },
        databento_api_key="dbn-test-key",
        ib_host="ib-gateway",
        ib_port=4002,
    )

    fresh_entry = next(
        (a for a in config.actors if "DataFreshnessActor" in a.actor_path),
        None,
    )
    assert fresh_entry is not None
    assert fresh_entry.config["bar_type_datasets"] == {
        "BRK.B.XNYS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI",
    }


def test_build_per_account_config_unmapped_venue_raises() -> None:
    """A native bar type whose venue has NO entry in venue_dataset_map must
    FAIL CLOSED at config-build time — a feed that can't be mapped to a dataset
    cannot silently lose freshness protection."""
    from msai.services.nautilus.live_node_config import (
        build_per_account_trading_node_config,
    )

    with pytest.raises(ValueError, match="venue_dataset_map"):
        build_per_account_trading_node_config(
            account_id="DUP733214",
            ibg_client_id=210,
            ib_login_key="marin1016test",
            native_instrument_ids=["AAPL.XNAS"],
            venue_dataset_map={"ARCX": "EQUS.MINI"},  # XNAS deliberately absent
            canonical_to_native_bar_types={
                "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
            },
            databento_api_key="dbn-test-key",
            ib_host="ib-gateway",
            ib_port=4002,
        )


def test_build_per_account_config_no_freshness_actor_when_canonical_empty() -> None:
    """Empty canonical_to_native_bar_types (legacy path) → NO freshness
    actor appended (the shim's own gate semantics are mirrored)."""
    from msai.services.nautilus.live_node_config import (
        build_per_account_trading_node_config,
    )

    config = build_per_account_trading_node_config(
        account_id="DUP733214",
        ibg_client_id=210,
        ib_login_key="marin1016test",
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
        canonical_to_native_bar_types={},
        databento_api_key="dbn-test-key",
        ib_host="ib-gateway",
        ib_port=4002,
    )

    assert not any("DataFreshnessActor" in a.actor_path for a in config.actors)


# ===========================================================================
# Task 4 — subprocess wiring: registry injection + monitor lifecycle +
# reconciled-marker DELETE/SET in the real ``run_subprocess_async`` run loop.
#
# These exercise the LIVE node entry point's freshness wiring without standing
# up a real Nautilus TradingNode or Postgres. A fake node exposes
# ``trader.actors()`` (so the run loop can retrieve the DataFreshnessActor and
# call set_registry) + ``kernel.clock`` (the monitor's clock); a no-op fake
# session factory satisfies the DB-write helpers; stub freshness/marker
# factories let us assert the run loop drives start/stop + clear/mark in order.
# ===========================================================================

import asyncio  # noqa: E402 — co-located with the Task-4 wiring tests
from typing import Any  # noqa: E402
from unittest.mock import patch  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

from msai.core.halt_keys import reconciled_key  # noqa: E402
from msai.services.nautilus.data_stale_monitor import DataStaleMonitor  # noqa: E402
from msai.services.nautilus.trading_node_subprocess import (  # noqa: E402
    TradingNodePayload,
    run_subprocess_async,
)


class _FakeClock:
    """Minimal node clock exposing ``timestamp_ns`` for the monitor."""

    def timestamp_ns(self) -> int:
        return 1_700_000_000_000_000_000


class _FakeTrader:
    def __init__(self, actors: list[Any]) -> None:
        self._actors = actors

    def actors(self) -> list[Any]:
        return list(self._actors)

    def strategies(self) -> list[Any]:
        return []


class _FakeKernel:
    def __init__(self) -> None:
        self.clock = _FakeClock()


class _FakeWiringNode:
    """Stand-in for ``TradingNode`` with just the surface the freshness
    wiring touches: ``build``, ``run_async``/``stop_async`` lifecycle,
    ``trader.actors()`` and ``kernel.clock``.

    ``run_async`` flips ``kernel.trader.is_running`` True (so
    ``wait_until_ready`` unblocks) and then blocks until ``stop_async``.
    """

    def __init__(self, actors: list[Any]) -> None:
        self.trader = _FakeTrader(actors)
        self.kernel = _FakeKernel()
        # wait_until_ready polls node.kernel.trader.is_running.
        self.kernel.trader = self.trader  # type: ignore[attr-defined]
        self.trader.is_running = False  # type: ignore[attr-defined]
        self._stop = asyncio.Event()

    def build(self) -> None:
        return None

    async def run_async(self) -> None:
        self.trader.is_running = True  # type: ignore[attr-defined]
        await self._stop.wait()

    async def stop_async(self) -> None:
        self._stop.set()

    def dispose(self) -> None:
        return None


class _FakeResult:
    """Minimal SQLAlchemy result stand-in for the subprocess DB helpers."""

    def scalar_one_or_none(self) -> Any:
        return None

    def scalar_one(self) -> Any:
        return None


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def begin(self) -> _FakeSession:
        return self

    async def execute(self, *args: Any, **kwargs: Any) -> _FakeResult:
        return _FakeResult()

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeSessionFactory:
    """No-op async session factory — the freshness wiring tests don't care
    about the DB writes (those have their own integration tests)."""

    def __call__(self) -> _FakeSession:
        return _FakeSession()


class _FakeRedis:
    """Records set/delete so the marker tests can assert lifecycle."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.deleted: list[str] = []
        self.set_calls: list[tuple[str, Any]] = []

    async def set(self, key: str, value: Any, **kwargs: Any) -> None:
        self.store[key] = value
        self.set_calls.append((key, value))

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self.deleted.append(k)
            self.store.pop(k, None)

    async def eval(self, *args: Any, **kwargs: Any) -> int:
        return 1

    async def aclose(self) -> None:
        return None


def _wiring_payload(*, data_freshness_enabled: bool = True) -> TradingNodePayload:
    return TradingNodePayload(
        row_id=uuid4(),
        deployment_id=uuid4(),
        deployment_slug="abcdef0123456789",
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={},
        ib_account_id="DUP733214",
        startup_health_timeout_s=2.0,
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        data_freshness_enabled=data_freshness_enabled,
    )


async def _drive_run_loop(
    *,
    node: _FakeWiringNode,
    payload: TradingNodePayload,
    monitor_factory: Any,
    marker_factory: Any,
) -> None:
    """Run ``run_subprocess_async`` to its ``running`` state, then trigger a
    clean stop so the finally block runs. Returns once the loop exits.

    The stop is driven by ``node.stop_async()`` (which unblocks the fake
    ``run_async``) — NOT by setting ``shutdown_event`` — so the run loop
    proceeds DETERMINISTICALLY through ``_mark_running`` → marker SET →
    monitor START → ``await node_run_task`` (clean exit) rather than racing
    the mid-startup shutdown checkpoints.
    """
    shutdown = asyncio.Event()

    async def _stopper() -> None:
        # Wait until the node is running, then a beat for the post-running
        # wiring (marker SET + monitor START) to complete, then unblock
        # ``run_async`` for a clean exit through the finally block.
        for _ in range(400):
            if getattr(node.trader, "is_running", False):
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        await node.stop_async()

    stopper = asyncio.create_task(_stopper())
    await run_subprocess_async(
        payload,
        session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
        node_factory=lambda _p: node,
        shutdown_event=shutdown,
        data_freshness_monitor_factory=monitor_factory,
        reconciled_marker_factory=marker_factory,
    )
    await stopper


@pytest.mark.asyncio
async def test_run_loop_injects_registry_into_actor_post_build() -> None:
    """The run loop must retrieve the DataFreshnessActor from
    ``node.trader.actors()`` and call ``set_registry`` with the shared
    registry it built — so the actor actually records observations."""
    actor = DataFreshnessActor(
        DataFreshnessActorConfig(
            native_bar_types=["AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"],
            bar_type_datasets={"AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI"},
        )
    )
    node = _FakeWiringNode(actors=[actor])
    payload = _wiring_payload()

    captured: dict[str, Any] = {}

    redis = _FakeRedis()

    async def _monitor_factory(p: TradingNodePayload, n: Any) -> DataStaleMonitor | None:
        # Build the real wiring the production factory will: registry +
        # actor retrieval + set_registry + derived required_feeds.
        from msai.services.live.data_freshness import FeedKey, FreshnessRegistry

        registry = FreshnessRegistry()
        for candidate in n.trader.actors():
            if isinstance(candidate, DataFreshnessActor):
                candidate.set_registry(registry)
                captured["registry"] = registry
        required = {
            FeedKey(dataset=ds, native_bar_type_str=bt)
            for bt, ds in {"AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI"}.items()
        }
        captured["required"] = required
        return DataStaleMonitor(
            registry=registry,
            required_feeds=required,
            redis_factory=lambda: redis,
            cfg=GraceConfig(),
            phase_resolver=resolve_session_phase,
            deployment_id=str(p.deployment_id),
            account_id=p.ib_account_id,
            node_id=p.deployment_slug,
            clock=n.kernel.clock,
        )

    async def _marker_factory(p: TradingNodePayload) -> Any:
        class _M:
            async def clear(self) -> None: ...
            async def mark_reconciled(self) -> None: ...

        return _M()

    await _drive_run_loop(
        node=node,
        payload=payload,
        monitor_factory=_monitor_factory,
        marker_factory=_marker_factory,
    )

    # The actor got the shared registry the factory built.
    assert "registry" in captured
    assert actor._registry is captured["registry"]


@pytest.mark.asyncio
async def test_run_loop_starts_and_stops_monitor() -> None:
    """The monitor is started after node start and stopped in the finally
    block — same discipline as the IBDisconnectHandler."""
    actor = DataFreshnessActor(DataFreshnessActorConfig())
    node = _FakeWiringNode(actors=[actor])
    payload = _wiring_payload()

    calls: list[str] = []

    class _StubMonitor:
        async def start(self, **kwargs: Any) -> None:
            calls.append("start")

        async def stop(self) -> None:
            calls.append("stop")

    async def _monitor_factory(p: TradingNodePayload, n: Any) -> Any:
        return _StubMonitor()

    async def _marker_factory(p: TradingNodePayload) -> Any:
        class _M:
            async def clear(self) -> None: ...
            async def mark_reconciled(self) -> None: ...

        return _M()

    await _drive_run_loop(
        node=node,
        payload=payload,
        monitor_factory=_monitor_factory,
        marker_factory=_marker_factory,
    )

    assert calls == ["start", "stop"]


@pytest.mark.asyncio
async def test_run_loop_empty_required_feeds_still_runs_monitor() -> None:
    """Legacy node (empty canonical_to_native_bar_types) → the monitor STILL
    runs with required_feeds=set() and publishes the EMPTY manifest. The run
    loop must NOT skip the monitor just because there are no Databento feeds."""
    actor = DataFreshnessActor(DataFreshnessActorConfig())
    node = _FakeWiringNode(actors=[actor])
    payload = TradingNodePayload(
        row_id=uuid4(),
        deployment_id=uuid4(),
        deployment_slug="abcdef0123456789",
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={},
        ib_account_id="DUP733214",
        startup_health_timeout_s=2.0,
        canonical_to_native_bar_types={},  # legacy → empty universe
        data_freshness_enabled=True,
    )

    seen_required: dict[str, Any] = {}
    calls: list[str] = []

    async def _monitor_factory(p: TradingNodePayload, n: Any) -> Any:
        required: set[Any] = set()  # legacy: empty
        seen_required["required"] = required

        class _M:
            async def start(self, **kwargs: Any) -> None:
                calls.append("start")

            async def stop(self) -> None:
                calls.append("stop")

        return _M()

    async def _marker_factory(p: TradingNodePayload) -> Any:
        class _M:
            async def clear(self) -> None: ...
            async def mark_reconciled(self) -> None: ...

        return _M()

    await _drive_run_loop(
        node=node,
        payload=payload,
        monitor_factory=_monitor_factory,
        marker_factory=_marker_factory,
    )

    assert calls == ["start", "stop"]
    assert seen_required["required"] == set()


@pytest.mark.asyncio
async def test_run_loop_monitor_factory_raising_fails_subprocess() -> None:
    """Codex iter-2 P1 — FAIL-CLOSED on monitor-wiring failure.

    When freshness is ENABLED and the monitor factory raises (a wiring bug),
    the subprocess must FAIL rather than keep trading with no freshness
    manifest + no data-stale protection. The exception propagates to the
    run-loop catch-all, which returns exit code 1 (failed) — same discipline
    as any other startup failure.

    FIX 2 (iter 8 — reordered): the monitor is built/started BEFORE
    ``_mark_running`` + the reconciled-marker SET. So a monitor-wiring failure
    now happens BEFORE the deployment is ever marked ``running`` — strictly
    more fail-closed than before. The marker is cleared (start-of-loop) but
    NEVER set (``mark_reconciled`` runs only AFTER a successful monitor start),
    and ``_mark_running`` is never reached. The node tears down via the finally
    block (``stop_async`` called)."""
    actor = DataFreshnessActor(DataFreshnessActorConfig())
    node = _FakeWiringNode(actors=[actor])
    payload = _wiring_payload()

    marker_order: list[str] = []
    mark_running_calls: list[UUID] = []

    async def _monitor_factory(p: TradingNodePayload, n: Any) -> Any:
        msg = "boom — monitor wiring bug"
        raise RuntimeError(msg)

    async def _marker_factory(p: TradingNodePayload) -> Any:
        class _M:
            async def clear(self) -> None:
                marker_order.append("clear")

            async def mark_reconciled(self) -> None:
                marker_order.append("mark")

        return _M()

    # Spy on _mark_running to prove the deployment is NEVER marked running when
    # the monitor wiring fails first (cheap, direct assertion of fail-closed).
    import msai.services.nautilus.trading_node_subprocess as tns_mod

    real_mark_running = tns_mod._mark_running

    async def _spy_mark_running(session_factory: Any, row_id: UUID) -> None:
        mark_running_calls.append(row_id)
        await real_mark_running(session_factory, row_id)

    shutdown = asyncio.Event()
    with patch.object(tns_mod, "_mark_running", _spy_mark_running):
        exit_code = await run_subprocess_async(
            payload,
            session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
            node_factory=lambda _p: node,
            shutdown_event=shutdown,
            data_freshness_monitor_factory=_monitor_factory,
            reconciled_marker_factory=_marker_factory,
        )

    # FAIL-CLOSED: the run must report a failure exit code, NOT a clean exit.
    assert exit_code == 1
    # The node must have been torn down (finally block ran stop_async).
    assert node._stop.is_set()
    # Marker cleared at start but NEVER set — the monitor start (which precedes
    # _mark_running + the marker SET) raised first.
    assert "clear" in marker_order
    assert "mark" not in marker_order
    # The deployment was NEVER marked running (monitor wiring failed first).
    assert mark_running_calls == []


@pytest.mark.asyncio
async def test_run_loop_monitor_start_initial_publish_failure_fails_subprocess() -> None:
    """Codex iter-12 P1 — FAIL-CLOSED on monitor START failure (not just factory).

    The factory SUCCEEDS and returns a real ``DataStaleMonitor``, but the
    monitor's ``start()`` raises during its synchronous initial feed-state
    publish (the per-feed pipeline execute fails after the manifest write). That
    failure must propagate exactly like a factory-raise: the subprocess FAILS
    (exit code 1), ``_mark_running`` is NEVER reached, and the reconciled marker
    is cleared-but-never-set. This is the start()-can-raise half of the iter-2
    fail-closed wiring — established by iter-12 so a half-published safety
    envelope never lets the node trade with stale leftover verdicts."""
    actor = DataFreshnessActor(
        DataFreshnessActorConfig(
            native_bar_types=["AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"],
            bar_type_datasets={"AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI"},
        )
    )
    node = _FakeWiringNode(actors=[actor])
    payload = _wiring_payload()

    marker_order: list[str] = []
    mark_running_calls: list[UUID] = []

    class _FailingPipeline:
        def __getattr__(self, _name: str) -> Any:
            return lambda *a, **k: None

        async def execute(self) -> Any:
            msg = "initial publish pipeline execute failed"
            raise RuntimeError(msg)

    class _StartFailRedis:
        async def set(self, *a: Any, **k: Any) -> None:
            # Manifest write succeeds; only the per-feed pipeline execute fails.
            return None

        def pipeline(self, *a: Any, **k: Any) -> Any:
            return _FailingPipeline()

        async def delete(self, *a: Any, **k: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

    async def _monitor_factory(p: TradingNodePayload, n: Any) -> DataStaleMonitor:
        registry = FreshnessRegistry()
        for candidate in n.trader.actors():
            if isinstance(candidate, DataFreshnessActor):
                candidate.set_registry(registry)
        required = {
            FeedKey(dataset=ds, native_bar_type_str=bt)
            for bt, ds in {"AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI"}.items()
        }
        return DataStaleMonitor(
            registry=registry,
            required_feeds=required,
            redis_factory=lambda: _StartFailRedis(),
            cfg=GraceConfig(),
            phase_resolver=resolve_session_phase,
            deployment_id=str(p.deployment_id),
            account_id=p.ib_account_id,
            node_id=p.deployment_slug,
            clock=n.kernel.clock,
        )

    async def _marker_factory(p: TradingNodePayload) -> Any:
        class _M:
            async def clear(self) -> None:
                marker_order.append("clear")

            async def mark_reconciled(self) -> None:
                marker_order.append("mark")

        return _M()

    import msai.services.nautilus.trading_node_subprocess as tns_mod

    real_mark_running = tns_mod._mark_running

    async def _spy_mark_running(session_factory: Any, row_id: UUID) -> None:
        mark_running_calls.append(row_id)
        await real_mark_running(session_factory, row_id)

    shutdown = asyncio.Event()
    with patch.object(tns_mod, "_mark_running", _spy_mark_running):
        exit_code = await run_subprocess_async(
            payload,
            session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
            node_factory=lambda _p: node,
            shutdown_event=shutdown,
            data_freshness_monitor_factory=_monitor_factory,
            reconciled_marker_factory=_marker_factory,
        )

    # FAIL-CLOSED: start()'s initial-publish failure must fail the subprocess.
    assert exit_code == 1
    assert node._stop.is_set()
    # Marker cleared at start, NEVER set (monitor start raised before the SET).
    assert "clear" in marker_order
    assert "mark" not in marker_order
    # The deployment was NEVER marked running (monitor start failed first).
    assert mark_running_calls == []


@pytest.mark.asyncio
async def test_run_loop_bad_grace_json_fails_subprocess_via_real_factory() -> None:
    """Codex iter-2 P1 — a bad ``DATA_FRESHNESS_GRACE_JSON`` in the payload
    makes ``GraceConfig.from_env_json`` raise INSIDE the production factory,
    which must FAIL the subprocess (exit code 1) — proving the test path is
    the real path (the factory itself calls ``from_env_json``)."""
    from msai.services.nautilus.trading_node_subprocess import (
        real_data_freshness_monitor_factory,
    )

    actor = DataFreshnessActor(
        DataFreshnessActorConfig(
            native_bar_types=["AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"],
            bar_type_datasets={"AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI"},
        )
    )
    node = _FakeWiringNode(actors=[actor])
    payload = TradingNodePayload(
        row_id=uuid4(),
        deployment_id=uuid4(),
        deployment_slug="abcdef0123456789",
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={},
        ib_account_id="DUP733214",
        startup_health_timeout_s=2.0,
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        data_freshness_enabled=True,
        redis_url="redis://localhost:6379/0",
        # Invalid JSON → GraceConfig.from_env_json raises ValueError inside
        # the real monitor factory.
        data_freshness_grace_json="{not valid json",
    )

    async def _marker_factory(p: TradingNodePayload) -> Any:
        class _M:
            async def clear(self) -> None: ...
            async def mark_reconciled(self) -> None: ...

        return _M()

    shutdown = asyncio.Event()
    exit_code = await run_subprocess_async(
        payload,
        session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
        node_factory=lambda _p: node,
        shutdown_event=shutdown,
        data_freshness_monitor_factory=real_data_freshness_monitor_factory,
        reconciled_marker_factory=_marker_factory,
    )

    assert exit_code == 1
    assert node._stop.is_set()


@pytest.mark.asyncio
async def test_run_loop_disabled_skips_monitor_and_marker() -> None:
    """data_freshness_enabled=False → NO monitor factory call, NO marker
    factory call. Existing flows are unaffected."""
    actor = DataFreshnessActor(DataFreshnessActorConfig())
    node = _FakeWiringNode(actors=[actor])
    payload = _wiring_payload(data_freshness_enabled=False)

    monitor_calls: list[str] = []
    marker_calls: list[str] = []

    async def _monitor_factory(p: TradingNodePayload, n: Any) -> Any:
        monitor_calls.append("built")
        return None

    async def _marker_factory(p: TradingNodePayload) -> Any:
        marker_calls.append("built")
        return None

    await _drive_run_loop(
        node=node,
        payload=payload,
        monitor_factory=_monitor_factory,
        marker_factory=_marker_factory,
    )

    assert monitor_calls == []
    assert marker_calls == []


@pytest.mark.asyncio
async def test_run_loop_clears_marker_before_start_and_sets_after_running() -> None:
    """The reconciled marker is DELETEd at subprocess start (before node
    start — restart re-arms fail-closed) and SET only after the node reaches
    ``running`` (i.e. after _mark_running). Order: clear → mark_reconciled."""
    actor = DataFreshnessActor(DataFreshnessActorConfig())
    node = _FakeWiringNode(actors=[actor])
    payload = _wiring_payload()

    order: list[str] = []

    async def _monitor_factory(p: TradingNodePayload, n: Any) -> Any:
        class _M:
            async def start(self, **kwargs: Any) -> None: ...
            async def stop(self) -> None: ...

        return _M()

    async def _marker_factory(p: TradingNodePayload) -> Any:
        class _M:
            async def clear(self) -> None:
                order.append("clear")

            async def mark_reconciled(self) -> None:
                order.append("mark")

        return _M()

    await _drive_run_loop(
        node=node,
        payload=payload,
        monitor_factory=_monitor_factory,
        marker_factory=_marker_factory,
    )

    assert order == ["clear", "mark"]


@pytest.mark.asyncio
async def test_run_loop_marker_absent_when_readiness_fails() -> None:
    """If the node never becomes ready (wait_until_ready times out), the
    marker is cleared at start but NEVER set — fail-closed. The deployment
    is not trustworthy without a healthy reconcile."""

    class _NeverReadyNode(_FakeWiringNode):
        async def run_async(self) -> None:
            # Never flip is_running → wait_until_ready times out.
            await self._stop.wait()

    actor = DataFreshnessActor(DataFreshnessActorConfig())
    node = _NeverReadyNode(actors=[actor])
    payload = _wiring_payload()
    # Short health timeout so the test doesn't hang.
    payload = TradingNodePayload(  # rebuild with short timeout
        row_id=payload.row_id,
        deployment_id=payload.deployment_id,
        deployment_slug=payload.deployment_slug,
        strategy_path=payload.strategy_path,
        strategy_config_path=payload.strategy_config_path,
        strategy_config=payload.strategy_config,
        ib_account_id=payload.ib_account_id,
        startup_health_timeout_s=0.3,
        native_instrument_ids=payload.native_instrument_ids,
        venue_dataset_map=payload.venue_dataset_map,
        canonical_to_native_bar_types=payload.canonical_to_native_bar_types,
        data_freshness_enabled=True,
    )

    order: list[str] = []

    async def _monitor_factory(p: TradingNodePayload, n: Any) -> Any:
        class _M:
            async def start(self, **kwargs: Any) -> None:
                order.append("monitor_start")

            async def stop(self) -> None: ...

        return _M()

    async def _marker_factory(p: TradingNodePayload) -> Any:
        class _M:
            async def clear(self) -> None:
                order.append("clear")

            async def mark_reconciled(self) -> None:
                order.append("mark")

        return _M()

    shutdown = asyncio.Event()
    await run_subprocess_async(
        payload,
        session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
        node_factory=lambda _p: node,
        shutdown_event=shutdown,
        data_freshness_monitor_factory=_monitor_factory,
        reconciled_marker_factory=_marker_factory,
    )

    assert "clear" in order
    assert "mark" not in order
    # Monitor must NOT be started either — it's wired after readiness.
    assert "monitor_start" not in order


def test_reconciled_key_shape_and_validation() -> None:
    """The reconciled marker key is deployment-scoped and rejects empty ids."""
    dep = str(uuid4())
    assert reconciled_key(dep) == f"msai:live:reconciled:{dep}"
    with pytest.raises(ValueError, match="deployment_id must be non-empty"):
        reconciled_key("")
