"""PR-1b data-freshness pure-domain layer.

In-node Databento feed-freshness detection for the live fleet. This
module is the PURE DOMAIN core — no Redis, no Nautilus runtime, no
threading beyond a single :class:`threading.Lock` for the registry. The
node-side monitor (a later task) drives :func:`evaluate` on a tick and
acts on the returned findings; this layer only decides *whether* a feed
or dataset is stale.

Four pieces:

* :class:`GraceConfig` — GRACE-ONLY config (pydantic), per
  ``(asset_class, phase)`` grace seconds, loadable from a JSON env
  override, fail-loud on bad input.
* :func:`resolve_session_phase` — composes
  ``services.trading_calendar`` calendars (XNYS / CMES) into the current
  trading phase plus that phase's open timestamp.
* :class:`FreshnessRegistry` — thread-safe last-event/last-arrival
  recorder, called from the Nautilus event thread.
* :func:`evaluate` — the staleness decision over a required-feed
  universe.

**Import discipline:** this module deliberately does NOT import
``nautilus_trader`` — that import installs a uvloop event-loop policy at
import time (see ``.claude/rules/nautilus.md`` gotcha #1) which breaks
arq on Python 3.12. The native ``BarType`` string is therefore parsed by
hand here (format ``<SYM>.<VENUE>-<STEP>-<AGGREGATION>-<PRICE>-<SOURCE>``).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today
from msai.services.trading_calendar import asset_class_to_exchange, get_calendar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from datetime import date

__all__ = [
    "AssetClass",
    "FeedKey",
    "FeedObservation",
    "FreshnessRegistry",
    "GraceConfig",
    "Phase",
    "SessionPhase",
    "StaleFinding",
    "asset_class_for_dataset",
    "evaluate",
    "parse_interval_s",
    "resolve_session_phase",
]

_NS_PER_S = 1_000_000_000

AssetClass = Literal["equity", "futures"]
Phase = Literal["rth", "pre", "post", "closed", "maintenance"]
Granularity = Literal["feed", "dataset"]

# Phases during which a feed CAN be judged stale. ``closed`` and
# ``maintenance`` are never-stale (no data is expected).
_OPEN_PHASES: frozenset[str] = frozenset({"rth", "pre", "post"})

# Equity pre/post windows anchored RELATIVE to the calendar session bounds
# so early-close days (13:00 ET half-days, encoded in session_close) shift
# the post window correctly.
_PRE_OFFSET_S = 5 * 3600 + 30 * 60  # session_open - 5h30m
_POST_OFFSET_S = 4 * 3600  # session_close + 4h

# GLBX daily maintenance gap, in CME-local (Chicago) wall-clock.
_MAINT_START_HOUR = 16  # 16:00 CT
_MAINT_END_HOUR = 17  # 17:00 CT

_NY_TZ = ZoneInfo("America/New_York")
_CT_TZ = ZoneInfo("America/Chicago")

# Time-aggregation → seconds. Unknown aggregation → fail-loud ValueError.
_AGGREGATION_S: dict[str, int] = {
    "SECOND": 1,
    "MINUTE": 60,
    "HOUR": 3600,
    "DAY": 86400,
}


# ---------------------------------------------------------------------------
# Bar-type parsing
# ---------------------------------------------------------------------------


# A native Nautilus BarType string is ``<INSTRUMENT_ID>-<SPEC>`` where the
# spec is ALWAYS exactly four dash-segments: ``STEP-AGGREGATION-PRICE-SOURCE``
# (e.g. ``1-MINUTE-LAST-EXTERNAL``). The instrument-id head may itself contain
# dashes/dots (``BRK.B.XNYS``), so step/aggregation are read from the END, not
# from a leading ``split('-')`` — that was the dotted-symbol parsing bug.
_SPEC_SEGMENTS = 4
_MIN_BAR_TYPE_SEGMENTS = _SPEC_SEGMENTS + 1  # instrument-id + 4-segment spec


def _split_bar_type(native_bar_type_str: str) -> tuple[str, str, str]:
    """Split a native BarType string into ``(instrument_id, step, aggregation)``.

    The bar-spec suffix is the LAST four dash-segments
    (``STEP-AGGREGATION-PRICE-SOURCE``); the instrument id is everything before
    it (``-`` re-joined). Raises ``ValueError`` for fewer than five dash
    segments — fail-loud by design.
    """
    parts = native_bar_type_str.split("-")
    if len(parts) < _MIN_BAR_TYPE_SEGMENTS:
        msg = f"Malformed bar-type string: {native_bar_type_str!r}"
        raise ValueError(msg)
    instrument_id = "-".join(parts[:-_SPEC_SEGMENTS])
    step, aggregation = parts[-_SPEC_SEGMENTS], parts[-_SPEC_SEGMENTS + 1]
    return instrument_id, step, aggregation


def parse_interval_s(native_bar_type_str: str) -> int:
    """Parse the bar cadence in seconds from a native Nautilus BarType string.

    Format: ``<SYM>.<VENUE>-<STEP>-<AGGREGATION>-<PRICE>-<SOURCE>`` — e.g.
    ``AAPL.XNAS-1-MINUTE-LAST-EXTERNAL`` or ``BRK.B.XNYS-1-MINUTE-LAST-EXTERNAL``.
    ``step * aggregation_seconds``; step/aggregation are read from the END so a
    dotted/dashed symbol prefix never shifts the indices.

    Raises ``ValueError`` for a malformed string or an unknown (non-time)
    aggregation (e.g. ``TICK``/``VOLUME``) — fail-loud by design.
    """
    _instrument_id, step_str, aggregation = _split_bar_type(native_bar_type_str)
    try:
        step = int(step_str)
    except ValueError as exc:
        msg = f"Malformed bar-type step in {native_bar_type_str!r}: {step_str!r}"
        raise ValueError(msg) from exc

    unit_s = _AGGREGATION_S.get(aggregation)
    if unit_s is None:
        msg = (
            f"Unknown / non-time bar aggregation {aggregation!r} in "
            f"{native_bar_type_str!r}; expected one of {sorted(_AGGREGATION_S)}"
        )
        raise ValueError(msg)

    return step * unit_s


def _symbol_from_bar_type(native_bar_type_str: str) -> str:
    """``AAPL.XNAS-1-MINUTE-LAST-EXTERNAL`` -> ``AAPL`` (venue stripped).

    Dotted share-class symbols survive intact: ``BRK.B.XNYS-...`` -> ``BRK.B``
    (the venue is stripped from the RIGHT via ``rsplit('.', 1)``). Raises
    ``ValueError`` for a malformed string (no dot in the instrument id, or
    fewer than five dash-segments).
    """
    instrument_id, _step, _aggregation = _split_bar_type(native_bar_type_str)
    if "." not in instrument_id:
        msg = f"Malformed bar-type instrument id (no venue) in {native_bar_type_str!r}"
        raise ValueError(msg)
    return instrument_id.rsplit(".", 1)[0]


# ---------------------------------------------------------------------------
# GraceConfig
# ---------------------------------------------------------------------------


def _default_grace() -> dict[str, dict[str, int | None]]:
    return {
        "equity": {"rth": 90, "pre": 300, "post": 300, "closed": None},
        "futures": {"rth": 120, "maintenance": None},
    }


def _default_grace_models() -> dict[AssetClass, _AssetGrace]:
    """Validated default grace models keyed by ``AssetClass``."""
    raw = _default_grace()
    return {
        "equity": _AssetGrace.model_validate(raw["equity"]),
        "futures": _AssetGrace.model_validate(raw["futures"]),
    }


class _AssetGrace(BaseModel):
    """Per-phase grace seconds for one asset class. Unknown phase keys are
    rejected (``extra="forbid"``); ``None`` means never-stale."""

    model_config = ConfigDict(extra="forbid")

    rth: int | None = None
    pre: int | None = None
    post: int | None = None
    closed: int | None = None
    maintenance: int | None = None


class GraceConfig(BaseModel):
    """GRACE-ONLY freshness config.

    ``grace`` holds per-``(asset_class, phase)`` grace seconds; defaults
    follow the PR-1b spec. ``expected_interval_s`` is NOT here — it is
    parsed per feed from the native BarType. ``interval_s`` is the monitor
    *tick* cadence scalar (default 5); ``startup_grace_s`` is the absent-
    feed grace at monitor start (default 180).

    Two load-time asserts (grace-only): ``startup_grace_s > 0`` and
    ``interval_s * 4 <= min(open-phase grace)``.
    """

    model_config = ConfigDict(extra="forbid")

    grace: dict[AssetClass, _AssetGrace] = Field(default_factory=_default_grace_models)
    startup_grace_s: int = 180
    interval_s: int = 5

    @model_validator(mode="after")
    def _check_asserts(self) -> GraceConfig:
        if self.startup_grace_s <= 0:
            msg = f"startup_grace_s must be > 0 (got {self.startup_grace_s})"
            raise ValueError(msg)

        open_graces = [
            g
            for asset in self.grace.values()
            for phase, g in asset.model_dump().items()
            if phase in _OPEN_PHASES and g is not None
        ]
        if open_graces:
            min_open = min(open_graces)
            if self.interval_s * 4 > min_open:
                msg = (
                    f"interval_s * 4 ({self.interval_s * 4}) must be <= "
                    f"min open-phase grace ({min_open})"
                )
                raise ValueError(msg)
        return self

    def grace_s(self, asset_class: AssetClass, phase: Phase) -> int | None:
        """Grace seconds for ``(asset_class, phase)``; ``None`` = never-stale."""
        asset = self.grace.get(asset_class)
        if asset is None:
            return None
        value: int | None = getattr(asset, phase)
        return value

    @classmethod
    def from_env_json(cls, raw: str | None) -> GraceConfig:
        """Build from an optional ``DATA_FRESHNESS_GRACE_JSON`` override.

        ``None``/empty → defaults. Invalid JSON or unknown keys → ``ValueError``
        (fail-loud at load). The override JSON may carry phase grace under
        ``equity``/``futures`` keys and/or top-level ``startup_grace_s`` /
        ``interval_s`` scalars; everything omitted keeps its default.
        """
        if not raw:
            return cls()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"Invalid DATA_FRESHNESS_GRACE_JSON: {exc}"
            raise ValueError(msg) from exc

        if not isinstance(parsed, dict):
            msg = "DATA_FRESHNESS_GRACE_JSON must be a JSON object"
            raise ValueError(msg)

        # Start from defaults, then overlay any provided phase grace so an
        # override that touches only one phase keeps the rest.
        merged: dict[str, dict[str, int | None]] = _default_grace()
        kwargs: dict[str, object] = {}

        for key, value in parsed.items():
            if key in ("startup_grace_s", "interval_s"):
                kwargs[key] = value
                continue
            if key not in merged:
                msg = f"Unknown asset class in grace override: {key!r}"
                raise ValueError(msg)
            if not isinstance(value, dict):
                msg = f"Grace override for {key!r} must be an object"
                raise ValueError(msg)
            # _AssetGrace(extra="forbid") rejects unknown phase keys.
            merged[key] = {**merged[key], **value}

        grace_models: dict[AssetClass, _AssetGrace] = {
            "equity": _AssetGrace.model_validate(merged["equity"]),
            "futures": _AssetGrace.model_validate(merged["futures"]),
        }
        return cls(grace=grace_models, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Session-phase resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionPhase:
    """Current trading phase plus that phase's open timestamp.

    ``phase_open_ts`` is epoch-ns of the moment the current OPEN phase began
    (used by :func:`evaluate`'s clamp); ``None`` for closed/maintenance.
    """

    phase: Phase
    phase_open_ts: int | None


def _to_ns(ts: datetime) -> int:
    return int(ts.timestamp() * _NS_PER_S)


def _resolve_equity_phase(now_utc: datetime, exchange_key: str) -> SessionPhase:
    cal = get_calendar(exchange_key)
    today = exchange_local_today_for(_NY_TZ)
    import pandas as pd

    today_ts = pd.Timestamp(today)
    if not cal.is_session(today_ts):
        return SessionPhase(phase="closed", phase_open_ts=None)

    session_open = cal.session_open(today_ts).to_pydatetime()
    session_close = cal.session_close(today_ts).to_pydatetime()
    pre_start = session_open.timestamp() - _PRE_OFFSET_S
    post_end = session_close.timestamp() + _POST_OFFSET_S
    now_s = now_utc.timestamp()
    open_s = session_open.timestamp()
    close_s = session_close.timestamp()

    if open_s <= now_s < close_s:
        return SessionPhase(phase="rth", phase_open_ts=_to_ns(session_open))
    if pre_start <= now_s < open_s:
        # Pre-market opens at pre_start.
        return SessionPhase(phase="pre", phase_open_ts=int(pre_start * _NS_PER_S))
    if close_s <= now_s < post_end:
        # Post-market opens at the session close.
        return SessionPhase(phase="post", phase_open_ts=_to_ns(session_close))
    return SessionPhase(phase="closed", phase_open_ts=None)


def _resolve_futures_phase(now_utc: datetime, exchange_key: str) -> SessionPhase:
    import contextlib

    import pandas as pd
    from exchange_calendars.errors import NotSessionError

    cal = get_calendar(exchange_key)

    # CMES sessions in exchange_calendars are LABELED by their CLOSE date and
    # run back-to-back: the session labeled D opens the prior trading day at
    # 17:00 CT and closes D at 17:00 CT. So a weekday-evening reopen (e.g.
    # Monday 17:30 CT) and the Sunday-evening reopen both fall inside the NEXT
    # day's session label — NOT inside the current local-date label, which has
    # already closed. We therefore find the session label whose
    # [session_open, session_close) window CONTAINS ``now`` by probing the
    # current local-date label AND the next session label.
    today = exchange_local_today_for(_CT_TZ)
    today_ts = pd.Timestamp(today)

    # Build the set of session labels whose window could contain ``now``:
    #  * the current local-date label (if today is a session) — covers the
    #    daytime/maintenance part of that session;
    #  * the session immediately FOLLOWING the current-date label — covers a
    #    weekday-evening reopen (e.g. Monday 17:30 CT is inside the Tuesday
    #    label, which opened Monday 17:00 CT);
    #  * the next session at/after today — covers a non-session-date evening
    #    reopen (Sunday/Saturday evening opens Monday's label).
    candidate_labels: list[pd.Timestamp] = []
    if cal.is_session(today_ts):
        candidate_labels.append(today_ts)
        # next_session can raise only at the calendar's far right bound (no
        # next session) — practically unreachable for wall-clock ``now``.
        with contextlib.suppress(NotSessionError):
            candidate_labels.append(cal.next_session(today_ts))
    else:
        # date_to_session(..., "next") accepts a non-session date and returns
        # the first session at/after it (Sunday → Monday, Saturday → Monday).
        candidate_labels.append(cal.date_to_session(today_ts, direction="next"))

    now_s = now_utc.timestamp()
    containing_open: datetime | None = None
    for label in candidate_labels:
        session_open = cal.session_open(label).to_pydatetime()
        session_close = cal.session_close(label).to_pydatetime()
        if session_open.timestamp() <= now_s < session_close.timestamp():
            containing_open = session_open
            break

    if containing_open is None:
        # No session window contains now (weekend daytime, post-Friday-close
        # before Sunday reopen, holiday closure). Nothing is expected.
        return SessionPhase(phase="closed", phase_open_ts=None)

    # Daily maintenance gap 16:00-17:00 CT, evaluated in Chicago wall-clock.
    now_ct = now_utc.astimezone(_CT_TZ)
    if _MAINT_START_HOUR <= now_ct.hour < _MAINT_END_HOUR:
        return SessionPhase(phase="maintenance", phase_open_ts=None)

    # phase_open_ts is the CONTAINING session's open so the phase-open clamp in
    # evaluate() works across the evening reopen.
    return SessionPhase(phase="rth", phase_open_ts=_to_ns(containing_open))


def exchange_local_today_for(tz: ZoneInfo) -> date:
    """Exchange-local 'today' in ``tz`` (Chicago/New-York), per the project's
    session-boundary discipline. Falls back to the shared CME helper when
    ``tz`` is Chicago so we reuse one source of truth there."""
    if tz is _CT_TZ:
        return exchange_local_today()
    return datetime.now(tz).date()


def resolve_session_phase(asset_class: AssetClass, now_utc: datetime) -> SessionPhase:
    """Resolve the current trading phase for ``asset_class`` at ``now_utc``.

    Equities compose XNYS sessions with pre/post windows anchored to the
    *actual* session bounds (so early-close days shift post correctly).
    Futures compose CMES sessions with the GLBX 16:00-17:00 CT maintenance
    gap; because CMES sessions are close-date-anchored and run back-to-back,
    the resolver finds the session label whose window CONTAINS ``now`` (the
    current local-date label OR the next session label), so weekday-evening
    and Sunday-evening reopens resolve as open phases. Boundaries are computed
    in exchange-local tz per the ``exchange_local_today()`` discipline.

    NOTE: ``now_utc`` MUST be ≈ wall-clock now (the monitor's only usage). The
    session DATE is derived from ``exchange_local_today_for`` (wall-clock now),
    while only ``now_utc``'s TIME-OF-DAY is compared against that day's session
    bounds. Passing a historical ``now_utc`` would mis-resolve — it would be
    matched against TODAY's session window, not its own date's.
    """
    exchange_key = asset_class_to_exchange(asset_class)
    if exchange_key is None:
        return SessionPhase(phase="closed", phase_open_ts=None)

    if asset_class == "futures":
        return _resolve_futures_phase(now_utc, exchange_key)
    return _resolve_equity_phase(now_utc, exchange_key)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedKey:
    """Identity of a single data feed: ``(dataset, native_bar_type_str)``.

    ``symbol`` and ``expected_interval_s`` are DERIVED from
    ``native_bar_type_str`` and computed once in :meth:`__post_init__` (rather
    than re-parsed on every property access — the monitor's per-bar hot path
    reads them repeatedly). They are ``compare=False`` so eq/hash still key on
    ``(dataset, native_bar_type_str)`` only.

    NOTE: a malformed ``native_bar_type_str`` raises ``ValueError`` at
    CONSTRUCTION time (the parse runs in ``__post_init__``), not on first
    attribute access."""

    dataset: str
    native_bar_type_str: str
    symbol: str = field(init=False, compare=False)
    expected_interval_s: int = field(init=False, compare=False)

    def __post_init__(self) -> None:
        # frozen dataclass — set the derived fields via object.__setattr__.
        object.__setattr__(self, "symbol", _symbol_from_bar_type(self.native_bar_type_str))
        object.__setattr__(self, "expected_interval_s", parse_interval_s(self.native_bar_type_str))


@dataclass(frozen=True, slots=True)
class FeedObservation:
    """The most recent event/arrival timestamps recorded for a feed (epoch-ns)."""

    ts_event_ns: int
    ts_arrival_ns: int


class FreshnessRegistry:
    """Thread-safe recorder of per-feed last-event/last-arrival timestamps.

    :meth:`record` is called from the Nautilus event thread; :meth:`snapshot`
    is called from the monitor thread. A single lock guards the dict. Only
    the latest (largest ``ts_event_ns``) observation per feed is retained.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._obs: dict[FeedKey, FeedObservation] = {}

    def record(self, feed_key: FeedKey, ts_event_ns: int, ts_arrival_ns: int) -> None:
        """Record an observation; keeps the newest event ts per feed."""
        with self._lock:
            existing = self._obs.get(feed_key)
            if existing is not None and existing.ts_event_ns >= ts_event_ns:
                return
            self._obs[feed_key] = FeedObservation(
                ts_event_ns=ts_event_ns, ts_arrival_ns=ts_arrival_ns
            )

    def snapshot(self) -> dict[FeedKey, FeedObservation]:
        """Return a shallow copy of the current observations."""
        with self._lock:
            return dict(self._obs)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaleFinding:
    """A single stale-feed or stale-dataset finding.

    ``granularity == "feed"`` carries ``feed_key``/``symbol``; a
    ``"dataset"`` finding aggregates over the dataset's feeds and may leave
    ``feed_key``/``symbol`` ``None``. ``last_event_ts`` is ``None`` for an
    absent (never-observed) required feed.
    """

    dataset: str
    feed_key: FeedKey | None
    symbol: str | None
    last_event_ts: int | None
    detected_at: int
    granularity: Granularity


def asset_class_for_dataset(dataset: str) -> AssetClass:
    """Futures datasets live under the GLBX/CMES symbology; everything else
    is treated as equity for phase resolution. PR-1b only wires EQUS.MINI
    (equity) and GLBX.MDP3 (futures, simulation-only)."""
    if dataset.upper().startswith("GLBX"):
        return "futures"
    return "equity"


# Private alias retained for the in-module evaluate() caller + any existing
# references; the public name is the canonical one (also used by the monitor).
_asset_class_for_dataset = asset_class_for_dataset


def evaluate(
    required_feeds: Iterable[FeedKey],
    snapshot: Mapping[FeedKey, FeedObservation],
    now_ns: int,
    monitor_started_ns: int,
    cfg: GraceConfig,
    phase_resolver: Callable[[AssetClass, datetime], SessionPhase],
) -> list[StaleFinding]:
    """Decide which required feeds / datasets are stale.

    A feed is stale iff, during an OPEN phase,
    ``now - max(last_event_ts, phase_open_ts) > expected_interval + grace``.

    The ``max(..., phase_open_ts)`` is the **phase-open clamp**: the budget
    restarts at phase open so a feed whose last event predates the open (a
    session reopen) is treated as warm until ``phase_open + interval +
    grace``. ACCEPTED COST: a feed that dies within one grace window of a
    phase boundary may go undetected until ``phase_open + interval + grace``
    of the *next* open phase — bounded, and safe because once a halt is
    latched no phase rollover un-halts it (there is NO was-stale latch
    carried across phases here; latching lives in the monitor layer).

    An ABSENT required feed (in ``required_feeds`` but missing from
    ``snapshot``) is stale once ``now - monitor_started_ns > startup_grace``
    during an open phase (manifest-minus-snapshot can auto-halt).

    A dataset is stale iff the max over its feeds' CLAMPED event ts exceeds
    the dataset budget (``min parsed interval among the dataset's feeds +
    grace``). CLOSED / maintenance phases never produce findings.
    """
    required = list(required_feeds)
    findings: list[StaleFinding] = []

    # Group required feeds by dataset so we can compute the per-dataset
    # min-interval budget and the dataset-level rollup.
    by_dataset: dict[str, list[FeedKey]] = {}
    for fk in required:
        by_dataset.setdefault(fk.dataset, []).append(fk)

    for dataset, feeds in by_dataset.items():
        asset_class = _asset_class_for_dataset(dataset)
        sp = phase_resolver(asset_class, _ns_to_utc(now_ns))
        if sp.phase not in _OPEN_PHASES:
            continue  # closed / maintenance → never stale

        grace = cfg.grace_s(asset_class, sp.phase)
        if grace is None:
            continue  # never-stale phase by config

        clamp = sp.phase_open_ts if sp.phase_open_ts is not None else 0

        # Track the most-recent CLAMPED event ts across the dataset's feeds
        # for the dataset-level staleness COMPARISON, plus the most-recent
        # REAL (unclamped) observed event ts for honest cause attribution in
        # the finding, and the dataset's min interval.
        dataset_min_interval = min(fk.expected_interval_s for fk in feeds)
        dataset_max_clamped: int | None = None
        dataset_max_real: int | None = None

        for fk in feeds:
            obs = snapshot.get(fk)
            interval_s = fk.expected_interval_s

            if obs is None:
                # Absent required feed: startup-grace gate.
                if now_ns - monitor_started_ns > cfg.startup_grace_s * _NS_PER_S:
                    findings.append(
                        StaleFinding(
                            dataset=dataset,
                            feed_key=fk,
                            symbol=fk.symbol,
                            last_event_ts=None,
                            detected_at=now_ns,
                            granularity="feed",
                        )
                    )
                continue

            clamped_event = max(obs.ts_event_ns, clamp)
            if dataset_max_clamped is None or clamped_event > dataset_max_clamped:
                dataset_max_clamped = clamped_event
            if dataset_max_real is None or obs.ts_event_ns > dataset_max_real:
                dataset_max_real = obs.ts_event_ns

            budget_ns = (interval_s + grace) * _NS_PER_S
            if now_ns - clamped_event > budget_ns:
                findings.append(
                    StaleFinding(
                        dataset=dataset,
                        feed_key=fk,
                        symbol=fk.symbol,
                        last_event_ts=obs.ts_event_ns,
                        detected_at=now_ns,
                        granularity="feed",
                    )
                )

        # Dataset-level rollup: if every observed feed is collectively silent
        # past the dataset budget (min interval + grace), emit a dataset
        # finding. The staleness COMPARISON uses the clamped ts (phase-open
        # clamp), but the finding reports the REAL most-recent observed event
        # ts (``dataset_max_real``) — honest cause attribution, not the clamp
        # (which may be phase_open_ts, not an actual event). Skip when no feed
        # was observed (covered by absent-feed).
        if dataset_max_clamped is not None:
            dataset_budget_ns = (dataset_min_interval + grace) * _NS_PER_S
            if now_ns - dataset_max_clamped > dataset_budget_ns:
                findings.append(
                    StaleFinding(
                        dataset=dataset,
                        feed_key=None,
                        symbol=None,
                        last_event_ts=dataset_max_real,
                        detected_at=now_ns,
                        granularity="dataset",
                    )
                )

    return findings


def _ns_to_utc(now_ns: int) -> datetime:
    return datetime.fromtimestamp(now_ns / _NS_PER_S, tz=UTC)
