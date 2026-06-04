"""Multi-failure-mode harness for the in-node data-stale monitor (PR 1b Task 3).

This is THE real-money safety test core satisfying council blocking
objection #9: it exercises the five PRD feed-failure modes (disconnect,
stale-timestamp, partial-dataset stall, single-symbol stall, reconnect-storm)
plus the negative cases (closing-bar gap, closed/maintenance phase, session
reopen, quiet-but-thin) and the monitor's Redis mechanics (manifest publish +
TTL, per-feed JSON + verdict companions, atomic Lua halt write with retry,
cause SETNX preservation, idempotent-while-stale, clean-stop cleanup, and
loop-never-raises-out).

No real sockets: Redis is ``fakeredis.aioredis.FakeRedis`` (Lua via the ``lua``
extra), the clock is an injected stub, and the freshness ``snapshot`` is built
directly in each test (the pure :class:`FreshnessRegistry` is exercised in
``test_data_freshness.py``). The monitor is driven a tick at a time via
``await monitor._tick()`` so each failure mode is deterministic.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from msai.core.halt_keys import (
    VERDICT_KEY_SUFFIX,
    data_freshness_key,
    data_freshness_manifest_key,
    fleet_halt_key,
    halt_cause_key,
)
from msai.services.live.data_freshness import (
    FeedKey,
    FeedObservation,
    GraceConfig,
    SessionPhase,
)
from msai.services.nautilus.data_stale_monitor import DataStaleMonitor

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

_NS_PER_S = 1_000_000_000

# Two equity feeds on the consolidated dataset + one futures feed.
EQ_AAPL = FeedKey(dataset="EQUS.MINI", native_bar_type_str="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
EQ_MSFT = FeedKey(dataset="EQUS.MINI", native_bar_type_str="MSFT.XNAS-1-MINUTE-LAST-EXTERNAL")
FUT_ES = FeedKey(dataset="GLBX.MDP3", native_bar_type_str="ESH6.GLBX-1-MINUTE-LAST-EXTERNAL")
# A thinner equity feed: 5-minute cadence.
EQ_SLOW = FeedKey(dataset="EQUS.MINI", native_bar_type_str="IWM.ARCX-5-MINUTE-LAST-EXTERNAL")


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _StubClock:
    """Monotonically advanceable wall-clock returning epoch-ns."""

    def __init__(self, start_ns: int) -> None:
        self.now_ns = start_ns

    def timestamp_ns(self) -> int:
        return self.now_ns

    def advance_s(self, seconds: float) -> None:
        self.now_ns += int(seconds * _NS_PER_S)


def _always_rth(_asset_class: str, _now: datetime) -> SessionPhase:
    """Phase resolver pinning the open timestamp far in the past so the
    phase-open clamp never masks a stale feed (each test controls staleness
    via the snapshot timestamps)."""
    return SessionPhase(phase="rth", phase_open_ts=0)


def _make_monitor(
    *,
    required_feeds: set[FeedKey],
    redis: Any,
    clock: _StubClock,
    phase_resolver: Callable[[str, datetime], SessionPhase] = _always_rth,
    cfg: GraceConfig | None = None,
    deployment_id: str = "dep-1",
    account_id: str = "DUP733214",
    node_id: str = "node-abc",
) -> DataStaleMonitor:
    return DataStaleMonitor(
        registry=None,  # tests inject snapshots directly; see _set_snapshot
        required_feeds=required_feeds,
        redis_factory=lambda: redis,
        cfg=cfg or GraceConfig(),
        phase_resolver=phase_resolver,
        deployment_id=deployment_id,
        account_id=account_id,
        node_id=node_id,
        clock=clock,
    )


def _set_snapshot(monitor: DataStaleMonitor, snapshot: dict[FeedKey, FeedObservation]) -> None:
    """Inject the snapshot the monitor's tick will evaluate, bypassing the live
    registry (which is exercised separately in test_data_freshness.py)."""
    monitor._snapshot_override = snapshot  # type: ignore[attr-defined]


def _obs(
    clock: _StubClock, event_age_s: float, arrival_age_s: float | None = None
) -> FeedObservation:
    """Observation whose last event was ``event_age_s`` ago (arrival defaults to
    the same instant unless overridden — stale-timestamp mode uses a recent
    arrival with an old event)."""
    if arrival_age_s is None:
        arrival_age_s = event_age_s
    return FeedObservation(
        ts_event_ns=clock.now_ns - int(event_age_s * _NS_PER_S),
        ts_arrival_ns=clock.now_ns - int(arrival_age_s * _NS_PER_S),
    )


async def _read(redis: Any, key: str) -> bytes | None:
    return await redis.get(key)


async def _read_str(redis: Any, key: str) -> str | None:
    raw = await redis.get(key)
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


@pytest.fixture
def clock() -> _StubClock:
    # Fixed start so timestamps are deterministic.
    return _StubClock(start_ns=1_900_000_000 * _NS_PER_S)


# ===========================================================================
# The FIVE PRD failure modes (blocking objection #9)
# ===========================================================================


@pytest.mark.asyncio
async def test_mode1_full_disconnect_halts_with_dataset_attribution(
    redis: Any, clock: _StubClock
) -> None:
    """Mode 1 — disconnect: ALL feeds silent past the dataset budget. Halt
    fires and a dataset-granularity cause is recorded."""
    monitor = _make_monitor(required_feeds={EQ_AAPL, EQ_MSFT}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    # Both 1-minute feeds silent for 10 minutes — well past 60s+90s budget.
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600), EQ_MSFT: _obs(clock, 600)})

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) == "true"
    cause = json.loads((await _read(redis, halt_cause_key("fleet"))).decode())
    assert cause["reason"] == "data_stale"
    assert cause["dataset"] == "EQUS.MINI"
    # A dataset-granularity finding is preferred for a full-dataset stall.
    history = await redis.lrange(halt_cause_key("fleet") + ":history", 0, -1)
    assert len(history) >= 1
    await monitor.stop()


@pytest.mark.asyncio
async def test_mode2_frozen_timestamp_halts(redis: Any, clock: _StubClock) -> None:
    """Mode 2 — stale-timestamp: arrivals keep coming (recent arrival) but the
    event timestamp is frozen past budget → still halts."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    # Arrival 1s ago (feed "alive") but event frozen 5 minutes ago.
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, event_age_s=300, arrival_age_s=1)})

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) == "true"
    cause = json.loads((await _read(redis, halt_cause_key("fleet"))).decode())
    assert cause["dataset"] == "EQUS.MINI"
    await monitor.stop()


@pytest.mark.asyncio
async def test_mode3_partial_dataset_stall_names_stalled_dataset(
    redis: Any, clock: _StubClock
) -> None:
    """Mode 3 — partial-dataset stall: EQUS.MINI silent, GLBX.MDP3 flowing.
    Halt names the STALLED dataset, not the flowing one."""
    monitor = _make_monitor(required_feeds={EQ_AAPL, FUT_ES}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(
        monitor,
        {EQ_AAPL: _obs(clock, 600), FUT_ES: _obs(clock, 5)},  # ES warm
    )

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) == "true"
    cause = json.loads((await _read(redis, halt_cause_key("fleet"))).decode())
    assert cause["dataset"] == "EQUS.MINI"
    await monitor.stop()


@pytest.mark.asyncio
async def test_mode4_single_symbol_stall_feed_attribution(redis: Any, clock: _StubClock) -> None:
    """Mode 4 — single-symbol stall: one feed stale, the other on the SAME
    dataset warm → halt with FEED-granularity attribution (names the symbol)."""
    monitor = _make_monitor(required_feeds={EQ_AAPL, EQ_MSFT}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(
        monitor,
        {EQ_AAPL: _obs(clock, 600), EQ_MSFT: _obs(clock, 5)},  # MSFT warm
    )

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) == "true"
    cause = json.loads((await _read(redis, halt_cause_key("fleet"))).decode())
    assert cause["dataset"] == "EQUS.MINI"
    assert cause["feed"] == EQ_AAPL.native_bar_type_str
    assert cause["symbol"] == "AAPL"
    await monitor.stop()


@pytest.mark.asyncio
async def test_mode5_reconnect_storm_does_not_halt(redis: Any, clock: _StubClock) -> None:
    """Mode 5 — reconnect-storm: repeated short gaps that each recover within
    grace. Across many ticks the feed is always within budget → NO halt."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    # Each tick the event is at most 40s old (< 60s interval + 90s rth grace).
    for age in (5, 40, 10, 35, 8, 40):
        clock.advance_s(5)
        _set_snapshot(monitor, {EQ_AAPL: _obs(clock, age)})
        await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) is None
    await monitor.stop()


# ===========================================================================
# Negative cases
# ===========================================================================


@pytest.mark.asyncio
async def test_closing_bar_natural_gap_no_halt(redis: Any, clock: _StubClock) -> None:
    """A 1-minute feed whose last bar is ~70s old sits inside its
    interval(60s)+grace(90s)=150s budget → no halt."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 70)})

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_closed_phase_no_halt(redis: Any, clock: _StubClock) -> None:
    """A closed/maintenance phase is never-stale even with ancient data."""

    def closed(_ac: str, _now: datetime) -> SessionPhase:
        return SessionPhase(phase="closed", phase_open_ts=None)

    monitor = _make_monitor(
        required_feeds={EQ_AAPL}, redis=redis, clock=clock, phase_resolver=closed
    )
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 100_000)})

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_maintenance_phase_no_halt(redis: Any, clock: _StubClock) -> None:
    """Futures GLBX maintenance window → never-stale."""

    def maint(_ac: str, _now: datetime) -> SessionPhase:
        return SessionPhase(phase="maintenance", phase_open_ts=None)

    monitor = _make_monitor(required_feeds={FUT_ES}, redis=redis, clock=clock, phase_resolver=maint)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {FUT_ES: _obs(clock, 100_000)})

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_session_reopen_clamped_no_halt(redis: Any, clock: _StubClock) -> None:
    """Session REOPEN: the feed's last event predates the phase open, but the
    clamp restarts the budget at phase_open → warm (no halt)."""
    # Phase opened 10s ago; the budget restarts there.
    phase_open_ns = clock.now_ns - 10 * _NS_PER_S

    def reopen(_ac: str, _now: datetime) -> SessionPhase:
        return SessionPhase(phase="rth", phase_open_ts=phase_open_ns)

    monitor = _make_monitor(
        required_feeds={EQ_AAPL}, redis=redis, clock=clock, phase_resolver=reopen
    )
    await monitor.start(run_loop=False)
    # Last event was 6 hours ago (overnight) — pre-open.
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 6 * 3600)})

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_quiet_thin_symbol_within_5min_interval_no_halt(
    redis: Any, clock: _StubClock
) -> None:
    """A 5-MINUTE feed whose last bar is 4 minutes old is within its
    parsed interval(300s)+grace → no halt (the budget honors the parsed
    bar cadence, not a flat 1-minute assumption)."""
    monitor = _make_monitor(required_feeds={EQ_SLOW}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_SLOW: _obs(clock, 240)})  # 4 min < 300+90

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_is_connected_never_consulted(redis: Any, clock: _StubClock) -> None:
    """The data-stale monitor judges freshness from event timestamps ONLY — it
    must never touch an ``is_connected`` attribute (that's the disconnect
    handler's job). Constructed without one, it still works."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    assert not hasattr(monitor, "is_connected")
    assert not hasattr(monitor, "_is_connected")


# ===========================================================================
# Manifest mechanics
# ===========================================================================


@pytest.mark.asyncio
async def test_manifest_published_at_start_with_ttl(redis: Any, clock: _StubClock) -> None:
    """At start the manifest is the SET of required FeedKeys serialized as a
    JSON list of {dataset, feed, symbol}, with a TTL of 3× the tick interval."""
    cfg = GraceConfig()
    monitor = _make_monitor(required_feeds={EQ_AAPL, FUT_ES}, redis=redis, clock=clock, cfg=cfg)
    await monitor.start(run_loop=False)

    key = data_freshness_manifest_key("dep-1")
    manifest = json.loads((await _read(redis, key)).decode())
    datasets = {entry["dataset"] for entry in manifest}
    feeds = {entry["feed"] for entry in manifest}
    assert datasets == {"EQUS.MINI", "GLBX.MDP3"}
    assert EQ_AAPL.native_bar_type_str in feeds
    ttl = await redis.ttl(key)
    assert 0 < ttl <= 3 * cfg.interval_s
    await monitor.stop()


@pytest.mark.asyncio
async def test_empty_manifest_for_no_required_feeds(redis: Any, clock: _StubClock) -> None:
    """A freshness-INELIGIBLE node (empty required_feeds — legacy path) STILL
    runs the monitor and publishes an EMPTY manifest (distinct from absent)."""
    monitor = _make_monitor(required_feeds=set(), redis=redis, clock=clock)
    await monitor.start(run_loop=False)

    key = data_freshness_manifest_key("dep-1")
    raw = await _read(redis, key)
    assert raw is not None  # present, not absent
    assert json.loads(raw.decode()) == []  # empty list
    await monitor.stop()


@pytest.mark.asyncio
async def test_manifest_reset_every_tick(redis: Any, clock: _StubClock) -> None:
    """Each tick RE-SETs the manifest with the same TTL so a SIGKILLed node's
    manifest self-expires while a live node keeps it fresh."""
    cfg = GraceConfig()
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock, cfg=cfg)
    await monitor.start(run_loop=False)
    key = data_freshness_manifest_key("dep-1")

    # Let the TTL decay, then tick and confirm it bumped back to full.
    await redis.expire(key, 1)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 5)})
    await monitor._tick()

    ttl = await redis.ttl(key)
    assert ttl > 1  # re-set on the tick
    await monitor.stop()


# ===========================================================================
# Per-feed publish mechanics
# ===========================================================================


@pytest.mark.asyncio
async def test_per_feed_json_and_verdict_published_every_tick(
    redis: Any, clock: _StubClock
) -> None:
    """Every tick publishes per-feed freshness JSON + a plain-string verdict
    companion, both for WARM feeds (so the API sees monitor liveness), with the
    correct TTLs and a published_at present."""
    cfg = GraceConfig()
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock, cfg=cfg)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 5)})  # warm

    await monitor._tick()

    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)
    payload = json.loads((await _read(redis, fkey)).decode())
    assert payload["verdict"] == "warm"
    assert payload["symbol"] == "AAPL"
    assert payload["account_id"] == "DUP733214"
    assert payload["node_id"] == "node-abc"
    assert payload["published_at"]  # ISO wall-clock present
    assert payload["last_event_ts"] is not None
    assert 0 < await redis.ttl(fkey) <= 3 * cfg.interval_s

    verdict = await _read_str(redis, fkey + VERDICT_KEY_SUFFIX)
    assert verdict == "warm"
    assert 0 < await redis.ttl(fkey + VERDICT_KEY_SUFFIX) <= 3 * cfg.interval_s
    await monitor.stop()


@pytest.mark.asyncio
async def test_stale_feed_publishes_stale_verdict(redis: Any, clock: _StubClock) -> None:
    """A stale feed publishes verdict 'stale' (the monitor publishes
    warm/pending/stale — 'missing' is API-derived)."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})

    await monitor._tick()

    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)
    assert await _read_str(redis, fkey + VERDICT_KEY_SUFFIX) == "stale"
    payload = json.loads((await _read(redis, fkey)).decode())
    assert payload["verdict"] == "stale"
    await monitor.stop()


@pytest.mark.asyncio
async def test_unobserved_feed_within_startup_grace_publishes_pending(
    redis: Any, clock: _StubClock
) -> None:
    """Codex iter-2 P1 — a REQUIRED feed with NO observation yet, still within
    startup grace, publishes verdict 'pending' (NOT 'warm'). This is the
    fail-closed signal that prevents /resume from clearing a halt before any
    data has been observed (e.g. a node restart DURING an outage)."""
    cfg = GraceConfig(startup_grace_s=180)
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock, cfg=cfg)
    await monitor.start(run_loop=False)
    # Empty snapshot → feed never observed. Tick within startup grace.
    _set_snapshot(monitor, {})

    await monitor._tick()

    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)
    assert await _read_str(redis, fkey + VERDICT_KEY_SUFFIX) == "pending"
    payload = json.loads((await _read(redis, fkey)).decode())
    assert payload["verdict"] == "pending"
    assert payload["last_event_ts"] is None
    # Within startup grace → NO halt fires (pending is not stale).
    assert await _read_str(redis, fleet_halt_key()) is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_pending_flips_to_warm_on_first_observation(redis: Any, clock: _StubClock) -> None:
    """Codex iter-2 P1 — once data arrives for a previously-unobserved feed,
    the verdict flips pending → warm (the feed is now resumable)."""
    cfg = GraceConfig(startup_grace_s=180)
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock, cfg=cfg)
    await monitor.start(run_loop=False)

    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)

    # Tick 1: unobserved within grace → pending.
    _set_snapshot(monitor, {})
    await monitor._tick()
    assert await _read_str(redis, fkey + VERDICT_KEY_SUFFIX) == "pending"

    # Tick 2: first observation arrives → warm.
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 5)})
    await monitor._tick()
    assert await _read_str(redis, fkey + VERDICT_KEY_SUFFIX) == "warm"
    await monitor.stop()


@pytest.mark.asyncio
async def test_pending_flips_to_stale_past_startup_grace(redis: Any, clock: _StubClock) -> None:
    """Codex iter-2 P1 — an unobserved required feed becomes 'stale' once the
    startup grace window elapses with no data (the absent-feed stale gate in
    ``evaluate``), so the halt fires fail-closed."""
    cfg = GraceConfig(startup_grace_s=180)
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock, cfg=cfg)
    await monitor.start(run_loop=False)

    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)

    # Tick 1: within grace → pending, no halt.
    _set_snapshot(monitor, {})
    await monitor._tick()
    assert await _read_str(redis, fkey + VERDICT_KEY_SUFFIX) == "pending"
    assert await _read_str(redis, fleet_halt_key()) is None

    # Advance past startup grace, still no observation → stale + halt.
    clock.advance_s(cfg.startup_grace_s + 10)
    await monitor._tick()
    assert await _read_str(redis, fkey + VERDICT_KEY_SUFFIX) == "stale"
    assert await _read_str(redis, fleet_halt_key()) == "true"
    await monitor.stop()


# ===========================================================================
# Synchronous start() publish + fast-restart verdict-reuse safety (iter-11)
# ===========================================================================


@pytest.mark.asyncio
async def test_start_publishes_pending_verdict_synchronously_before_any_tick(
    redis: Any, clock: _StubClock
) -> None:
    """Codex iter-11 High — start() SYNCHRONOUSLY publishes per-feed rows +
    verdict companions for every required feed BEFORE any tick. An unobserved
    feed at start (empty registry) publishes 'pending', not absent — so the
    state is authoritative the instant start() returns."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    # Unobserved at start (no snapshot injected → empty).
    _set_snapshot(monitor, {})

    await monitor.start(run_loop=False)

    # No _tick() called yet — the verdict must already be present + 'pending'.
    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)
    assert await _read_str(redis, fkey + VERDICT_KEY_SUFFIX) == "pending"
    payload = json.loads((await _read(redis, fkey)).decode())
    assert payload["verdict"] == "pending"
    assert payload["last_event_ts"] is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_fast_restart_overwrites_stale_warm_verdicts_at_start(
    redis: Any, clock: _StubClock
) -> None:
    """Codex iter-11 High — fast-restart simulation. Seed the OLD run's 'warm'
    verdict keys + manifest for the (stable) deployment id, then construct a
    NEW monitor and start() it. The synchronous start-publish OVERWRITES the
    stale 'warm' verdicts with 'pending' (unobserved at restart) BEFORE the new
    monitor ever ticks — so a /resume reaching the row in the restart window
    can no longer clear a halt off the previous run's warm verdicts."""
    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)
    vkey = fkey + VERDICT_KEY_SUFFIX
    mkey = data_freshness_manifest_key("dep-1")

    # OLD run left 'warm' verdict keys + manifest alive (e.g. a crash, or just
    # before TTL expiry on a fast restart).
    await redis.set(vkey, "warm")
    await redis.set(fkey, json.dumps({"verdict": "warm"}))
    await redis.set(mkey, json.dumps([{"dataset": "EQUS.MINI", "feed": "x", "symbol": "AAPL"}]))

    # NEW monitor for the SAME deployment id, unobserved at restart.
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    _set_snapshot(monitor, {})
    await monitor.start(run_loop=False)

    # The stale 'warm' verdict has been OVERWRITTEN to 'pending' at start time.
    assert await _read_str(redis, vkey) == "pending"
    payload = json.loads((await _read(redis, fkey)).decode())
    assert payload["verdict"] == "pending"
    await monitor.stop()


@pytest.mark.asyncio
async def test_initial_publish_failure_raises_out_of_start(redis: Any, clock: _StubClock) -> None:
    """Codex iter-12 P1 — the initial feed-state publish at start() must be
    FAIL-CLOSED. If the synchronous start-time per-feed publish fails (e.g. the
    pipeline execute raises) AFTER the manifest write, start() must RAISE rather
    than swallow it. Swallowing it would let the run-loop reach ``_mark_running``
    + the reconciled marker while the OLD run's TTL-alive ``warm`` verdicts
    survive — breaking the overwrite-before-running invariant, so ``/resume``
    could clear a halt off a prior run's verdicts. The wiring then fails the
    subprocess BEFORE ``_mark_running`` (the monitor-wiring-failure path)."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    _set_snapshot(monitor, {})

    boom = RuntimeError("redis pipeline execute failed")

    class _FailingPipeline:
        def __getattr__(self, _name: str) -> Any:
            # Queueing commands (set/...) is a no-op; execute is what fails.
            return lambda *a, **k: None

        async def execute(self) -> Any:
            raise boom

    # Manifest write (plain set) succeeds; the per-feed pipeline execute fails.
    with (
        patch.object(redis, "pipeline", lambda *a, **k: _FailingPipeline()),
        pytest.raises(RuntimeError, match="redis pipeline execute failed"),
    ):
        await monitor.start(run_loop=False)

    # No background loop was ever spawned (start raised before that).
    assert monitor._task is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tick_publish_failure_is_swallowed_by_loop(redis: Any, clock: _StubClock) -> None:
    """Steady-state resilience is UNCHANGED: a per-tick publish failure must NOT
    crash the live node. The loop's own try/except keeps swallowing tick errors
    (only start()'s INITIAL publish is fail-closed). This is the counterpart to
    test_initial_publish_failure_raises_out_of_start — same failure shape, but
    raised from inside _tick() must be swallowed."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 5)})

    class _FailingPipeline:
        def __getattr__(self, _name: str) -> Any:
            return lambda *a, **k: None

        async def execute(self) -> Any:
            raise RuntimeError("tick pipeline boom")

    with patch.object(redis, "pipeline", lambda *a, **k: _FailingPipeline()):
        # Must NOT raise out of the tick.
        await monitor._tick()

    await monitor.stop()


# ===========================================================================
# Halt-write mechanics (atomic Lua + retry + SETNX preservation)
# ===========================================================================


@pytest.mark.asyncio
async def test_halt_written_via_lua_with_24h_ttl(redis: Any, clock: _StubClock) -> None:
    """The latch + companions are written via the atomic script with a 24h TTL
    and the set_by names the monitor + node."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})

    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) == "true"
    ttl = await redis.ttl(fleet_halt_key())
    assert 86000 < ttl <= 86400
    set_by = await _read_str(redis, fleet_halt_key() + ":set_by")
    assert "data_stale_monitor" in set_by
    assert "node-abc" in set_by
    assert await _read_str(redis, fleet_halt_key() + ":set_at")
    await monitor.stop()


@pytest.mark.asyncio
async def test_halt_retries_on_first_failure(redis: Any, clock: _StubClock) -> None:
    """The script call is retried with backoff on a transient Redis error;
    the SECOND attempt succeeds and the halt lands."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})

    calls = {"n": 0}
    real_eval = redis.eval

    async def flaky_eval(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("redis blip")
        return await real_eval(*args, **kwargs)

    import msai.services.nautilus.data_stale_monitor as mod

    real_backoff = mod.HALT_SET_BACKOFF_S
    mod.HALT_SET_BACKOFF_S = 0.001  # type: ignore[assignment]
    try:
        with patch.object(redis, "eval", flaky_eval):
            await monitor._tick()
    finally:
        mod.HALT_SET_BACKOFF_S = real_backoff  # type: ignore[assignment]

    assert calls["n"] == 2
    assert await _read_str(redis, fleet_halt_key()) == "true"
    await monitor.stop()


@pytest.mark.asyncio
async def test_halt_preserves_existing_manual_cause_and_appends_history(
    redis: Any, clock: _StubClock
) -> None:
    """A pre-existing manual /kill-all cause is PRESERVED (SET NX) while the
    data-stale cause is still appended to the capped history list."""
    # Seed an existing manual cause.
    await redis.set(halt_cause_key("fleet"), json.dumps({"reason": "fleet_emergency"}))

    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})

    await monitor._tick()

    # Cause key still the manual one (unchanged).
    cause = json.loads((await _read(redis, halt_cause_key("fleet"))).decode())
    assert cause["reason"] == "fleet_emergency"
    # History grew with the data_stale cause.
    history = await redis.lrange(halt_cause_key("fleet") + ":history", 0, -1)
    parsed = [json.loads(h.decode()) for h in history]
    assert any(c["reason"] == "data_stale" for c in parsed)
    await monitor.stop()


@pytest.mark.asyncio
async def test_idempotent_while_stale_no_history_spam(redis: Any, clock: _StubClock) -> None:
    """While a feed stays stale, repeated ticks do NOT re-write the halt /
    re-LPUSH the cause history (one fire per warm→stale transition)."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})

    await monitor._tick()
    hist_len_1 = await redis.llen(halt_cause_key("fleet") + ":history")

    # Second tick, still stale.
    clock.advance_s(5)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 605)})
    await monitor._tick()
    hist_len_2 = await redis.llen(halt_cause_key("fleet") + ":history")

    assert hist_len_1 == hist_len_2  # no re-LPUSH
    await monitor.stop()


@pytest.mark.asyncio
async def test_one_metric_per_transition(redis: Any, clock: _StubClock) -> None:
    """The stale-halt metric increments exactly ONCE per warm→stale transition,
    not on every tick while stale."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)

    import msai.services.observability.trading_metrics as metrics

    with patch.object(metrics.DATA_STALE_HALTS, "inc") as inc:
        _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})
        await monitor._tick()
        clock.advance_s(5)
        _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 605)})
        await monitor._tick()

    assert inc.call_count == 1
    await monitor.stop()


@pytest.mark.asyncio
async def test_halt_fire_incrs_redis_data_stale_halts_counter(
    redis: Any, clock: _StubClock
) -> None:
    """On a warm→stale transition that fires the halt, the monitor INCRs the
    plain Redis ``data_stale_halts`` counter keyed by account — the
    Prometheus-visible source of truth (the in-process registry counter lives
    in the SUBPROCESS, invisible to the API /metrics registry)."""
    from msai.core.halt_keys import data_stale_halts_key

    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})

    await monitor._tick()

    counter = await _read_str(redis, data_stale_halts_key("DUP733214"))
    assert counter == "1"

    # Idempotent while stale: a second stale tick does NOT re-INCR.
    clock.advance_s(5)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 605)})
    await monitor._tick()
    counter2 = await _read_str(redis, data_stale_halts_key("DUP733214"))
    assert counter2 == "1"
    await monitor.stop()


@pytest.mark.asyncio
async def test_keeps_publishing_warm_after_recovery_without_clearing_latch(
    redis: Any, clock: _StubClock
) -> None:
    """After a feed recovers the monitor publishes warm verdicts again BUT does
    NOT clear the halt latch — resume is operator-only."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})
    await monitor._tick()
    assert await _read_str(redis, fleet_halt_key()) == "true"

    # Feed recovers.
    clock.advance_s(5)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 2)})
    await monitor._tick()

    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)
    assert await _read_str(redis, fkey + VERDICT_KEY_SUFFIX) == "warm"
    # Latch still set — NOT cleared by recovery.
    assert await _read_str(redis, fleet_halt_key()) == "true"
    await monitor.stop()


@pytest.mark.asyncio
async def test_re_stale_after_operator_resume_re_latches(redis: Any, clock: _StubClock) -> None:
    """Monitor re-arm: a stale episode latches the fleet; the feed recovers
    (warm tick re-arms the flag); the operator clears the latch; a LATER stale
    episode in the SAME node lifetime FIRES AGAIN (second eval + LPUSH). Without
    re-arming this would fail-OPEN after a /resume."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)

    # Episode 1 — stale → halt fires.
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})
    await monitor._tick()
    assert await _read_str(redis, fleet_halt_key()) == "true"
    hist_len_1 = await redis.llen(halt_cause_key("fleet") + ":history")
    assert hist_len_1 == 1

    # Feed recovers (fully-warm tick re-arms the episode flag).
    clock.advance_s(5)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 2)})
    await monitor._tick()

    # Operator clears the latch + cause keyset between episodes (the /resume
    # success path — simulated here as a plain delete).
    await redis.delete(
        fleet_halt_key(),
        fleet_halt_key() + ":set_by",
        fleet_halt_key() + ":set_at",
        halt_cause_key("fleet"),
        halt_cause_key("fleet") + ":history",
    )
    assert await _read_str(redis, fleet_halt_key()) is None

    # Episode 2 — re-stales. The monitor must FIRE AGAIN (re-latch + LPUSH).
    clock.advance_s(5)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})
    await monitor._tick()

    assert await _read_str(redis, fleet_halt_key()) == "true"
    cause = json.loads((await _read(redis, halt_cause_key("fleet"))).decode())
    assert cause["reason"] == "data_stale"
    hist_len_2 = await redis.llen(halt_cause_key("fleet") + ":history")
    assert hist_len_2 == 1  # fresh history list, re-LPUSHed on the new episode
    await monitor.stop()


@pytest.mark.asyncio
async def test_re_stale_with_latch_not_cleared_still_appends_history(
    redis: Any, clock: _StubClock
) -> None:
    """Monitor re-arm, latch NOT cleared: after recovery + re-arm, a second
    stale episode re-evaluates and the SET-NX cause stays the first cause while
    the new data_stale cause is appended to history — acceptable per
    HALT_WRITE_LUA semantics (SET NX preserves cause, LPUSH always appends)."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)

    # Episode 1 — stale → halt fires (one history entry).
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})
    await monitor._tick()
    hist_len_1 = await redis.llen(halt_cause_key("fleet") + ":history")
    assert hist_len_1 == 1

    # Feed recovers (re-arms) but the operator does NOT clear the latch.
    clock.advance_s(5)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 2)})
    await monitor._tick()
    assert await _read_str(redis, fleet_halt_key()) == "true"  # still latched

    # Episode 2 — re-stales. _fire_halt runs again (flag re-armed), appending a
    # second history entry; the cause key is preserved by SET NX.
    clock.advance_s(5)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 600)})
    await monitor._tick()

    hist_len_2 = await redis.llen(halt_cause_key("fleet") + ":history")
    assert hist_len_2 == 2  # LPUSH always appends — acceptable per HALT semantics
    cause = json.loads((await _read(redis, halt_cause_key("fleet"))).decode())
    assert cause["reason"] == "data_stale"  # preserved (SET NX)
    await monitor.stop()


# ===========================================================================
# Lifecycle: clean-stop cleanup + loop-never-raises-out
# ===========================================================================


@pytest.mark.asyncio
async def test_clean_stop_deletes_per_feed_keys_but_leaves_manifest_ttl_alive(
    redis: Any, clock: _StubClock
) -> None:
    """On clean stop() (Codex iter-11 High) the monitor DELETES the per-feed
    JSON + ``:verdict`` keys but LEAVES the manifest to TTL-expire.

    WHY delete per-feed keys: deployment ids are stable across restarts, so a
    leftover ``warm`` verdict could let ``/resume`` clear a halt off a previous
    run's state before the new monitor observes anything. Deleting them on
    clean stop removes that fuel.

    WHY leave the manifest: deleting it on a still-``running`` row was iter-10's
    false-page bug — the subprocess stops the monitor in its ``finally`` BEFORE
    the terminal write flips the row out of ``running``, so an absent manifest
    would falsely page ``monitor_missing``. Deleting ONLY the per-feed keys does
    NOT page (monitor-missing keys off the MANIFEST). The manifest stays
    TTL-alive (TTL > 0, ≤ 3× the tick interval) and self-expires shortly
    after."""
    cfg = GraceConfig(interval_s=5)
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock, cfg=cfg)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 5)})
    await monitor._tick()

    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)
    vkey = fkey + VERDICT_KEY_SUFFIX
    mkey = data_freshness_manifest_key("dep-1")
    assert await _read(redis, fkey) is not None

    await monitor.stop()

    # Per-feed JSON + verdict are DELETED (no fuel for a fast-restart /resume).
    assert await _read(redis, fkey) is None
    assert await _read(redis, vkey) is None
    # The manifest STILL EXISTS (no eager delete) and carries a positive TTL
    # bounded by 3× the tick interval, so it self-expires shortly after the
    # terminal write flips the row.
    assert await _read(redis, mkey) is not None
    ttl = await redis.ttl(mkey)
    max_ttl = 3 * cfg.interval_s
    assert 0 < ttl <= max_ttl, f"{mkey} TTL {ttl} not in (0, {max_ttl}]"


@pytest.mark.asyncio
async def test_running_row_with_freshly_stopped_monitor_is_not_monitor_missing(
    redis: Any, clock: _StubClock
) -> None:
    """Data-health invariant: a 'running' row whose monitor has just been
    stop()ped is NOT reported ``monitor_missing``, because the manifest is
    still TTL-alive in the shutdown window.

    This is the data-health-shaped assertion for the iter-10 fix: the
    data-health collector keys monitor-missing off an ABSENT manifest for a
    running deployment. Since clean stop() no longer eagerly deletes the
    manifest, the collector still sees it present during the shutdown window
    and does NOT page."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 5)})
    await monitor._tick()
    await monitor.stop()

    # The collector's monitor-missing predicate for a 'running' row: manifest
    # absent (None) → page. After a clean stop the manifest is still present,
    # so the predicate is False (no page).
    raw_manifest = await redis.get(data_freshness_manifest_key("dep-1"))
    monitor_missing_for_running_row = raw_manifest is None
    assert monitor_missing_for_running_row is False


@pytest.mark.asyncio
async def test_evaluator_exception_does_not_crash_loop(redis: Any, clock: _StubClock) -> None:
    """If the evaluation step raises, the tick logs and swallows it — the loop
    never raises out (a freshness bug must not crash the live node)."""
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock)
    await monitor.start(run_loop=False)

    with patch(
        "msai.services.nautilus.data_stale_monitor.evaluate",
        side_effect=RuntimeError("boom"),
    ):
        _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 5)})
        # Must NOT raise.
        await monitor._tick()

    # No halt fired (the evaluation failed, but the loop survived).
    assert await _read_str(redis, fleet_halt_key()) is None
    await monitor.stop()


@pytest.mark.asyncio
async def test_run_loop_ticks_until_stopped(redis: Any, clock: _StubClock) -> None:
    """The injected-loop run() ticks on the interval cadence and exits cleanly
    when stop() is called (mirrors IBDisconnectHandler's start/stop lifecycle)."""
    cfg = GraceConfig(interval_s=5)
    monitor = _make_monitor(required_feeds={EQ_AAPL}, redis=redis, clock=clock, cfg=cfg)
    _set_snapshot(monitor, {EQ_AAPL: _obs(clock, 5)})

    # Drive the real loop with a tiny tick interval so the test is fast.
    await monitor.start(tick_interval_s=0.01)
    await asyncio.sleep(0.05)

    # While the loop is RUNNING, it has published the manifest + at least one
    # per-feed key.
    fkey = data_freshness_key("dep-1", "EQUS.MINI", EQ_AAPL.native_bar_type_str)
    assert await _read(redis, data_freshness_manifest_key("dep-1")) is not None
    assert await _read(redis, fkey) is not None

    await monitor.stop()
    # The loop exited cleanly (the task is gone). On clean stop the per-feed
    # keys ARE deleted (iter-11 — no stale verdict fuel for a fast restart),
    # while the manifest stays TTL-alive through the shutdown window (see
    # test_clean_stop_deletes_per_feed_keys_but_leaves_manifest_ttl_alive).
    assert monitor._task is None  # type: ignore[attr-defined]
    assert await _read(redis, fkey) is None
    assert await _read(redis, data_freshness_manifest_key("dep-1")) is not None
