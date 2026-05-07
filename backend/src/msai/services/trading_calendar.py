"""Trading-day calendar service.

Maps an MSAI asset class to a single ``exchange_calendars`` key and
returns the set of trading days in a date range. Cached per-process
because ``exchange_calendars`` calendar construction is non-trivial
and trading-day membership is queried inside the day-precise coverage
scan (one call per `(symbol, window)` row on the inventory page).

Asset-class → calendar map:

    equity / stocks / option   → XNYS  (NYSE)
    futures                    → CMES  (CME Globex)
    fx                         → XNYS  (FX is OTC 24/5; NYSE schedule is the
                                        closest match — we don't trade FX
                                        through stock holidays anyway)
    crypto                     → None  (24/7 — fall back to weekday-only via
                                        pandas.bdate_range)
    unknown asset class        → None  (same fall-back; logs a warning)

The module is import-safe: ``exchange_calendars`` is imported lazily
inside the cached factory so a misconfigured environment doesn't break
process startup.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

import pandas as pd

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from datetime import date

log = get_logger(__name__)

__all__ = ["asset_class_to_exchange", "trading_days"]


_ASSET_CLASS_TO_EXCHANGE: dict[str, str] = {
    # Ingest-taxonomy keys (what compute_coverage actually receives —
    # callers normalize via normalize_asset_class_for_ingest before
    # invoking the scan; see services/nautilus/security_master/types.py
    # REGISTRY_TO_INGEST_ASSET_CLASS):
    "stocks": "XNYS",
    "options": "XNYS",
    "forex": "XNYS",  # FX is OTC 24/5; NYSE schedule is the closest match
    "futures": "CMES",
    # Registry-taxonomy keys — accepted defensively for callers that
    # bypass the normalizer (tests, ad-hoc scripts):
    "equity": "XNYS",
    "option": "XNYS",
    "fx": "XNYS",
}


def asset_class_to_exchange(asset_class: str) -> str | None:
    """Return the ``exchange_calendars`` key for an asset class, or
    ``None`` for asset classes without a recognized exchange calendar
    (crypto, unknown). The caller falls back to weekday-only filtering
    via ``pandas.bdate_range`` for ``None``."""
    return _ASSET_CLASS_TO_EXCHANGE.get(asset_class.lower())


@lru_cache(maxsize=8)
def _calendar(exchange_key: str) -> Any:
    """Cached calendar instance. Lazy import of ``exchange_calendars``
    so missing-dep doesn't break process startup."""
    import exchange_calendars as ec

    return ec.get_calendar(exchange_key)


def trading_days(start: date, end: date, *, asset_class: str) -> set[date]:
    """Return the set of trading days (inclusive of both endpoints) for
    ``asset_class``'s calendar. Falls back to weekday-only filter when
    no exchange is mapped (crypto, unknown).

    Empty set when ``start > end``.
    """
    if start > end:
        return set()

    exchange_key = asset_class_to_exchange(asset_class)
    if exchange_key is None:
        # Weekday-only fall-back. ``bdate_range`` excludes Sat/Sun.
        idx = pd.bdate_range(start=start, end=end)
        return {ts.date() for ts in idx}

    cal = _calendar(exchange_key)
    sessions = cal.sessions_in_range(
        pd.Timestamp(start),
        pd.Timestamp(end),
    )
    return {ts.date() for ts in sessions}
