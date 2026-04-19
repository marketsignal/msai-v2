"""Trading-hours extractor + Nautilus instrument passthrough
(Phase 2 task 2.4).

Nautilus's :class:`InteractiveBrokersInstrumentProvider` already
handles the heavy lifting of converting IB ``ContractDetails`` into
Nautilus ``Equity`` / ``FuturesContract`` / ``OptionContract`` /
``CurrencyPair`` objects via its internal ``parse_instrument``
function. Per the "use Nautilus API, never reinvent" rule, this
module does NOT wrap that path — :class:`IBQualifier` (task 2.3)
already returns the parsed ``Instrument`` straight from the provider.

What this module OWNS:

- :func:`extract_trading_hours` — pure function that parses IB's
  ``tradingHours`` / ``liquidHours`` strings plus ``timeZoneId``
  into the JSONB schema documented on the ``InstrumentCache`` model
  (see ``msai/models/instrument_cache.py`` module docstring).

- :func:`nautilus_instrument_to_cache_json` — serializes a Nautilus
  ``Instrument`` object into a JSONB-compatible dict for the
  ``nautilus_instrument_json`` column. Uses Nautilus's built-in
  ``to_dict`` classmethod (``Instrument`` and all its subclasses
  have ``to_dict()`` / ``from_dict()``).

IB trading-hours format reference:

Before IB API 9.72, ``tradingHours`` was a semicolon-separated list
of sessions in the form::

    20250601:0930-20250601:1600;20250602:0930-20250602:1600

For days the venue is closed, the field reads::

    20250525:CLOSED

Our extractor:

1. Splits on ``;`` into per-date sessions.
2. For each session, parses the open/close times and converts the
   date to a day-of-week code (``MON``/``TUE``/``WED``/``THU``/``FRI``/
   ``SAT``/``SUN``).
3. Deduplicates across dates so the result is a per-weekday
   template — IB gives us a week's worth of sessions, most of which
   repeat for most venues.
4. Emits the canonical ``{timezone, rth, eth}`` schema.

Returns ``None`` when both hours strings are empty — used for
venues with no meaningful session boundary (continuous forex on
IDEALPRO, 24/7 crypto).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


_DAY_OF_WEEK_CODES: dict[int, str] = {
    0: "MON",
    1: "TUE",
    2: "WED",
    3: "THU",
    4: "FRI",
    5: "SAT",
    6: "SUN",
}
"""Python's ``date.weekday()`` returns 0=Monday → 6=Sunday. This
maps that integer to the uppercase three-letter code used in the
``trading_hours`` JSONB schema so downstream consumers (Phase 4's
market-hours guard) can match day names directly."""


def _parse_ib_hours_string(hours: str) -> list[dict[str, str]]:
    """Parse an IB ``tradingHours`` / ``liquidHours`` string into
    per-weekday sessions.

    Format::

        20250601:0930-20250601:1600;20250602:0930-20250602:1600;20250525:CLOSED

    Returns a list of ``{"day": "MON", "open": "09:30", "close": "16:00"}``
    dicts, deduplicated so each weekday appears at most once. Dates
    marked ``CLOSED`` are skipped (we only emit sessions where the
    venue was open).

    Invalid / unparseable entries are skipped with no error — IB's
    format has historical quirks and we prefer "partial data" to
    "no data" here. The caller's :func:`extract_trading_hours`
    still returns ``None`` if NEITHER the RTH nor ETH string
    produced any sessions.
    """
    if not hours:
        return []

    from datetime import datetime

    seen: set[tuple[str, str, str]] = set()
    sessions: list[dict[str, str]] = []

    for session_str in hours.split(";"):
        session_str = session_str.strip()
        if not session_str or ":CLOSED" in session_str:
            continue

        # Parse ``20250601:0930-20250601:1600``
        try:
            open_part, close_part = session_str.split("-", 1)
            open_date_str, open_time_str = open_part.split(":", 1)
            _close_date_str, close_time_str = close_part.split(":", 1)
            d = datetime.strptime(open_date_str, "%Y%m%d").date()
            day = _DAY_OF_WEEK_CODES[d.weekday()]
            # IB times are HHMM — convert to HH:MM for the schema.
            open_hhmm = f"{open_time_str[:2]}:{open_time_str[2:4]}"
            close_hhmm = f"{close_time_str[:2]}:{close_time_str[2:4]}"
        except (ValueError, KeyError, IndexError):
            continue

        key = (day, open_hhmm, close_hhmm)
        if key in seen:
            continue
        seen.add(key)
        sessions.append({"day": day, "open": open_hhmm, "close": close_hhmm})

    return sessions


def extract_trading_hours(
    *,
    trading_hours: str | None,
    liquid_hours: str | None,
    time_zone_id: str | None,
) -> dict[str, Any] | None:
    """Convert IB ``ContractDetails`` trading-hours fields into the
    ``trading_hours`` JSONB schema the ``instrument_cache`` column
    expects.

    Args:
        trading_hours: The raw ``ContractDetails.tradingHours``
            string. Covers RTH + ETH sessions on IB's rules.
        liquid_hours: The raw ``ContractDetails.liquidHours``
            string. RTH only.
        time_zone_id: The venue time zone (e.g.
            ``America/New_York``, ``US/Central``).

    Returns:
        ``{"timezone": ..., "rth": [...], "eth": [...]}`` if at
        least one session was parseable. ``None`` if neither hours
        string produced any sessions — used for 24h venues (forex,
        crypto) where a session structure isn't meaningful.
    """
    liquid_sessions = _parse_ib_hours_string(liquid_hours or "")
    all_sessions = _parse_ib_hours_string(trading_hours or "")

    # ``trading_hours`` includes RTH; we derive ETH as
    # ``trading_hours - liquid_hours``. Normalise comparison keys.
    liquid_keys = {(s["day"], s["open"], s["close"]) for s in liquid_sessions}
    eth_sessions = [s for s in all_sessions if (s["day"], s["open"], s["close"]) not in liquid_keys]

    if not liquid_sessions and not eth_sessions:
        return None

    return {
        "timezone": time_zone_id or "UTC",
        "rth": liquid_sessions,
        "eth": eth_sessions,
    }


def nautilus_instrument_to_cache_json(instrument: Instrument) -> dict[str, Any]:
    """Serialize a Nautilus ``Instrument`` object to a
    JSONB-compatible dict for the ``nautilus_instrument_json``
    column.

    Nautilus's ``Instrument`` base class provides ``to_dict(self)``
    (``nautilus_trader/model/instruments/base.pyx``) that produces
    a flat dict of strings/numbers — directly serializable to
    JSONB without any additional encoding.

    We do NOT write our own serialization here. If Nautilus changes
    the shape of ``to_dict()``, the cache schema travels with it —
    which is exactly the contract we want (backtest and live both
    read from the cache via ``Instrument.from_dict(row)``, so any
    drift between the writer and reader paths is automatically
    reconciled when we upgrade Nautilus).
    """
    return instrument.to_dict(instrument)
