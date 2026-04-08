"""MarketHoursService — read trading hours from
``instrument_cache.trading_hours`` and answer "is this
instrument tradeable right now?" (Phase 4 task 4.3).

The :class:`RiskAwareStrategy` mixin (Phase 3 task 3.7) takes
an optional ``_market_hours_check`` callable; this module
provides the production wiring. The callable signature is
``(InstrumentId) -> bool`` — synchronous because the strategy
is on Nautilus's hot path and can't await.

Implementation notes:

- The service caches the trading-hours JSON in memory once
  loaded, refreshed lazily on first access per instrument
  per process. Phase 2's instrument cache rarely changes, so
  the in-memory snapshot is good enough — a future task
  will add a periodic refresh against the DB.
- Times are interpreted in the cache row's ``timezone`` field
  using ``zoneinfo``. We do NOT call ``pytz`` (deprecated as
  of Python 3.9) — the stdlib zoneinfo is the right choice
  for modern Python.
- ``is_in_rth`` answers "Regular Trading Hours". ``is_in_eth``
  answers "Extended Hours". RTH is a subset of ETH for any
  reasonable schedule, but we don't enforce that — the
  caller asks the question they need.
- A row with ``trading_hours = NULL`` (forex on a 24h venue,
  continuous futures, etc.) is treated as "always open".
  Better to let the order through than to halt every forex
  strategy because the metadata isn't populated.

The service is stateful (the in-memory cache) and is
constructed once per process. It is not thread-safe — Nautilus
calls into the strategy from a single thread per node, so
contention isn't a concern.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession


log = logging.getLogger(__name__)


_DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
"""Indices match Python's ``datetime.weekday()`` (Monday = 0).
Trading hours JSON uses 3-letter uppercase day names per the
Phase 2 schema."""


def _parse_hhmm(value: str) -> time:
    """Parse a ``"HH:MM"`` string into a ``datetime.time``."""
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _is_in_window(
    ts: datetime,
    windows: list[dict[str, str]],
    timezone: str,
) -> bool:
    """Return True if ``ts`` (any timezone) falls inside one
    of the ``windows`` defined in the cache row's
    ``timezone``. Windows are ``{day, open, close}`` dicts;
    ``ts`` is converted to the row's timezone before
    comparison so DST transitions are handled correctly.

    Cross-midnight windows (open > close, e.g.
    ``open=18:00, close=01:00``) are handled by the
    wrap-around branch — Nautilus's IB hours parser can
    emit such windows for futures sessions that span
    midnight. The window is considered active when:

    - The day matches AND ``open <= time < close`` (normal
      same-day window), OR
    - The day matches AND ``time >= open`` (we're in the
      after-midnight tail that started TODAY but ends
      TOMORROW), OR
    - The PREVIOUS day matches AND ``time < close`` (we're
      in the after-midnight tail of YESTERDAY's session).

    Codex batch 10 P2 fix.
    """
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001
        log.warning("market_hours_unknown_timezone", extra={"timezone": timezone})
        return True  # fail open — better than blocking every order

    local = ts.astimezone(tz)
    day_name = _DAY_NAMES[local.weekday()]
    prev_day_name = _DAY_NAMES[(local.weekday() - 1) % 7]
    local_time = local.time()

    for window in windows:
        win_day = window.get("day")
        if win_day not in (day_name, prev_day_name):
            continue
        try:
            open_t = _parse_hhmm(window["open"])
            close_t = _parse_hhmm(window["close"])
        except (KeyError, ValueError):
            log.warning("market_hours_bad_window", extra={"window": window})
            continue

        if open_t <= close_t:
            # Normal same-day window — only matches when
            # win_day == today
            if win_day == day_name and open_t <= local_time < close_t:
                return True
        else:
            # Cross-midnight window: open > close.
            # Today's match: we're past today's open, in
            # the pre-midnight tail
            if win_day == day_name and local_time >= open_t:
                return True
            # Yesterday's match: we're in the post-midnight
            # tail of yesterday's session
            if win_day == prev_day_name and local_time < close_t:
                return True
    return False


class MarketHoursService:
    """Per-process service that loads trading hours from the
    ``instrument_cache`` table and answers RTH/ETH questions.

    Construction is async because the cache primer hits the
    DB. The instance is then used synchronously from the hot
    path (Nautilus strategy callbacks).
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any] | None] = {}
        """canonical_id → trading_hours JSON or None for "no
        hours data"."""

    async def prime(self, session: AsyncSession, canonical_ids: list[str]) -> None:
        """Pre-load trading hours for ``canonical_ids`` from the
        ``instrument_cache`` table. Call once at deployment
        startup with the strategy's universe so the
        synchronous read path never blocks on a DB call.
        """
        from sqlalchemy import select

        from msai.models.instrument_cache import InstrumentCache

        result = await session.execute(
            select(InstrumentCache.canonical_id, InstrumentCache.trading_hours).where(
                InstrumentCache.canonical_id.in_(canonical_ids)
            )
        )
        for canonical_id, trading_hours in result:
            self._cache[canonical_id] = trading_hours

        # Anything we asked for but didn't find — record as
        # "no data" so the synchronous reader doesn't keep
        # logging cache-miss warnings.
        for canonical_id in canonical_ids:
            if canonical_id not in self._cache:
                self._cache[canonical_id] = None

    def is_in_rth(self, canonical_id: str, ts: datetime) -> bool:
        """True if ``ts`` falls inside the instrument's
        regular trading hours. Treats unknown instruments
        and instruments with no trading-hours data as always
        open (fail open) — better than blocking every order
        on a metadata gap."""
        return self._is_in_window_kind(canonical_id, ts, "rth")

    def is_in_eth(self, canonical_id: str, ts: datetime) -> bool:
        """True if ``ts`` falls inside the instrument's
        extended trading hours. Same fail-open semantics as
        :meth:`is_in_rth`."""
        return self._is_in_window_kind(canonical_id, ts, "eth")

    def _is_in_window_kind(self, canonical_id: str, ts: datetime, kind: str) -> bool:
        hours = self._cache.get(canonical_id)
        if hours is None:
            # Not primed OR explicitly null — fail open
            return True
        timezone = hours.get("timezone", "America/New_York")
        windows = hours.get(kind) or []
        if not windows:
            return True  # no schedule for this kind → fail open
        return _is_in_window(ts, windows, timezone)


def make_market_hours_check(
    service: MarketHoursService,
    *,
    allow_eth: bool = False,
) -> Callable[[Any], bool]:
    """Build the synchronous callable the
    :class:`RiskAwareStrategy` mixin expects. The callable
    receives a Nautilus ``InstrumentId`` and returns ``True``
    if trading is allowed right now.

    ``allow_eth`` is a per-strategy flag from the deployment
    config. ``False`` (default) requires the order to be
    inside RTH. ``True`` accepts any time inside ETH.
    Futures strategies typically set ``allow_eth=True``;
    equity day-trading strategies leave it ``False``.

    The callable evaluates ``datetime.now()`` at each call —
    not at strategy startup — so a strategy that runs
    overnight gets the right answer for the bar it's
    actually processing.
    """

    def check(instrument_id: Any) -> bool:
        canonical_id = str(instrument_id)
        now = datetime.now(UTC)
        if allow_eth:
            return service.is_in_eth(canonical_id, now)
        return service.is_in_rth(canonical_id, now)

    return check
