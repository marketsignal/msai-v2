"""``OrderIntent`` extraction (Phase 2 task 2.11).

The parity contract between backtest and live is intentionally
narrow: given the SAME bars, the strategy must emit the SAME
``(decision_timestamp, instrument_id, side, signed_qty)`` tuples
in the same order. Slippage, latency, fill price, partial fills,
and broker rejections are all OUT of the parity contract — those
are runtime concerns the Phase 5 paper soak catches, not the
backtest harness.

This module owns the conversion from a Nautilus
``BacktestResult.orders_df`` (or any DataFrame in the same shape)
to a list of :class:`OrderIntent` tuples. The :mod:`comparator`
module compares two intent sequences for exact ordered equality.

The shape of ``orders_df`` (verified against Nautilus 1.223.0
``Trader.generate_orders_report``) has these columns we care
about:

- ``ts_init`` / ``init_time`` — int64 nanoseconds since epoch when
  the order was constructed (the strategy's decision timestamp).
  We use this — NOT ``ts_event`` — because the decision time is
  what the parity contract anchors on.
- ``instrument_id`` — Nautilus canonical id string.
- ``order_side`` — ``"BUY"`` or ``"SELL"`` string.
- ``quantity`` — string-encoded ``Quantity`` (Nautilus stringifies
  these for DataFrame round-tripping).

Different Nautilus versions sometimes rename ``ts_init`` to
``init_time`` or ``ts_init_ns``. The normalizer is column-name
flexible: it tries each candidate in priority order and raises
:class:`KeyError` only if none are present.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


_TS_CANDIDATE_COLUMNS: tuple[str, ...] = (
    "ts_init",
    "ts_init_ns",
    "init_time",
    "ts_event",
)
"""Columns to try, in priority order, when looking for the
decision timestamp. Nautilus has renamed this column twice across
1.20x → 1.22x; the normalizer handles all three names."""

_INSTRUMENT_CANDIDATE_COLUMNS: tuple[str, ...] = (
    "instrument_id",
    "instrument",
)

_SIDE_CANDIDATE_COLUMNS: tuple[str, ...] = (
    "order_side",
    "side",
)

_QUANTITY_CANDIDATE_COLUMNS: tuple[str, ...] = (
    "quantity",
    "qty",
)


@dataclass(slots=True, frozen=True)
class OrderIntent:
    """The strategy's intent at order-submission time.

    Frozen + slotted so equality + hashability are stable across
    Python versions, which matters because :func:`compare` does
    list-equality comparisons. Equal-by-value semantics: two
    intents with the same fields compare equal regardless of how
    they were constructed.
    """

    decision_timestamp: datetime
    instrument_id: str
    side: str  # "BUY" or "SELL"
    signed_qty: Decimal


def _resolve_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str:
    """Pick the first candidate column name that exists in the
    DataFrame, or raise ``KeyError`` listing all the names tried."""
    for name in candidates:
        if name in df.columns:
            return name
    raise KeyError(
        f"none of the candidate columns {candidates} found in DataFrame columns {tuple(df.columns)}"
    )


def _ts_to_datetime(value: object) -> datetime:
    """Convert a Nautilus timestamp value (int nanoseconds OR
    pandas Timestamp OR str ISO) to a UTC ``datetime``. Robust to
    the three Nautilus version conventions."""
    import pandas as pd

    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, pd.Timestamp):
        ts = value.tz_convert(UTC) if value.tz is not None else value.tz_localize(UTC)
        py_dt: datetime = ts.to_pydatetime()
        return py_dt
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    raise TypeError(f"unsupported timestamp value type: {type(value).__name__}")


def normalize_orders_df(orders_df: pd.DataFrame) -> list[OrderIntent]:
    """Convert a Nautilus orders report DataFrame into a sorted
    list of :class:`OrderIntent` tuples.

    The output is sorted by ``decision_timestamp`` ascending so the
    comparator can do a positional comparison without worrying
    about Nautilus's internal row ordering (which can vary by
    venue / engine version).

    Empty DataFrames produce an empty list — that's a valid result
    (a strategy that didn't trade in the window), not an error.

    Args:
        orders_df: DataFrame from
            ``Trader.generate_orders_report()``. Column names are
            resolved flexibly via the candidate lists at the top
            of this module.

    Returns:
        A list of :class:`OrderIntent` tuples sorted by
        decision_timestamp.

    Raises:
        KeyError: If none of the timestamp / instrument / side /
            quantity candidate columns are present.
    """
    if orders_df.empty:
        return []

    ts_col = _resolve_column(orders_df, _TS_CANDIDATE_COLUMNS)
    instrument_col = _resolve_column(orders_df, _INSTRUMENT_CANDIDATE_COLUMNS)
    side_col = _resolve_column(orders_df, _SIDE_CANDIDATE_COLUMNS)
    quantity_col = _resolve_column(orders_df, _QUANTITY_CANDIDATE_COLUMNS)

    intents: list[OrderIntent] = []
    for _, row in orders_df.iterrows():
        side_str = str(row[side_col]).upper()
        if side_str not in {"BUY", "SELL"}:
            raise ValueError(
                f"unrecognized order side {row[side_col]!r} — expected 'BUY' or 'SELL'"
            )
        qty = Decimal(str(row[quantity_col]))
        signed = qty if side_str == "BUY" else -qty
        intents.append(
            OrderIntent(
                decision_timestamp=_ts_to_datetime(row[ts_col]),
                instrument_id=str(row[instrument_col]),
                side=side_str,
                signed_qty=signed,
            )
        )

    intents.sort(key=lambda intent: (intent.decision_timestamp, intent.instrument_id))
    return intents
