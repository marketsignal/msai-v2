"""Unit tests for the parity normalizer + comparator
(Phase 2 task 2.11)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from msai.services.nautilus.parity.comparator import (
    DivergenceKind,
    compare,
    is_identical,
)
from msai.services.nautilus.parity.normalizer import (
    OrderIntent,
    normalize_orders_df,
)

# ---------------------------------------------------------------------------
# normalize_orders_df
# ---------------------------------------------------------------------------


def _make_orders_df(rows: list[dict]) -> pd.DataFrame:
    """Helper that builds a Nautilus-shaped orders DataFrame from
    a list of plain dicts. Tests use this so they can express
    the input shape clearly."""
    return pd.DataFrame(rows)


class TestNormalizeOrdersDf:
    def test_empty_dataframe_returns_empty_list(self) -> None:
        """A strategy that didn't trade in the window produces
        an empty result — that's a valid output, not an error."""
        result = normalize_orders_df(pd.DataFrame())
        assert result == []

    def test_single_buy_order(self) -> None:
        df = _make_orders_df(
            [
                {
                    "ts_init": 1_700_000_000_000_000_000,  # 2023-11-14 22:13:20 UTC
                    "instrument_id": "AAPL.NASDAQ",
                    "order_side": "BUY",
                    "quantity": "10",
                }
            ]
        )
        result = normalize_orders_df(df)
        assert len(result) == 1
        intent = result[0]
        assert intent.instrument_id == "AAPL.NASDAQ"
        assert intent.side == "BUY"
        assert intent.signed_qty == Decimal("10")
        assert intent.decision_timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)

    def test_sell_order_produces_negative_signed_qty(self) -> None:
        """The signed-quantity contract: SELL → negative,
        BUY → positive. Caller-friendly because the comparator
        sees a single signed value rather than (side, qty)
        pairs."""
        df = _make_orders_df(
            [
                {
                    "ts_init": 1_700_000_000_000_000_000,
                    "instrument_id": "MSFT.NASDAQ",
                    "order_side": "SELL",
                    "quantity": "5",
                }
            ]
        )
        result = normalize_orders_df(df)
        assert result[0].signed_qty == Decimal("-5")

    def test_decimal_quantity_preserved(self) -> None:
        """Quantity strings can carry fractional sizes (forex,
        crypto). The Decimal conversion preserves precision."""
        df = _make_orders_df(
            [
                {
                    "ts_init": 1_700_000_000_000_000_000,
                    "instrument_id": "EUR/USD.IDEALPRO",
                    "order_side": "BUY",
                    "quantity": "12345.67",
                }
            ]
        )
        result = normalize_orders_df(df)
        assert result[0].signed_qty == Decimal("12345.67")

    def test_multiple_orders_sorted_by_timestamp(self) -> None:
        """Output is sorted by ``decision_timestamp`` ascending so
        positional comparison in the comparator is order-stable."""
        df = _make_orders_df(
            [
                # Deliberately reversed
                {
                    "ts_init": 2_000_000_000_000_000_000,
                    "instrument_id": "MSFT.NASDAQ",
                    "order_side": "BUY",
                    "quantity": "1",
                },
                {
                    "ts_init": 1_000_000_000_000_000_000,
                    "instrument_id": "AAPL.NASDAQ",
                    "order_side": "SELL",
                    "quantity": "2",
                },
            ]
        )
        result = normalize_orders_df(df)
        assert [i.instrument_id for i in result] == ["AAPL.NASDAQ", "MSFT.NASDAQ"]

    def test_alternative_column_name_init_time(self) -> None:
        """Older Nautilus versions used ``init_time``. The
        normalizer's column-resolver picks it up."""
        df = _make_orders_df(
            [
                {
                    "init_time": 1_700_000_000_000_000_000,
                    "instrument_id": "AAPL.NASDAQ",
                    "order_side": "BUY",
                    "quantity": "1",
                }
            ]
        )
        result = normalize_orders_df(df)
        assert len(result) == 1

    def test_alternative_column_name_side(self) -> None:
        df = _make_orders_df(
            [
                {
                    "ts_init": 1_700_000_000_000_000_000,
                    "instrument_id": "AAPL.NASDAQ",
                    "side": "BUY",
                    "quantity": "1",
                }
            ]
        )
        result = normalize_orders_df(df)
        assert result[0].side == "BUY"

    def test_pandas_timestamp_input(self) -> None:
        """Nautilus sometimes hands back pandas Timestamps
        instead of int nanoseconds. The normalizer accepts both."""
        df = _make_orders_df(
            [
                {
                    "ts_init": pd.Timestamp("2023-11-14 22:13:20", tz="UTC"),
                    "instrument_id": "AAPL.NASDAQ",
                    "order_side": "BUY",
                    "quantity": "1",
                }
            ]
        )
        result = normalize_orders_df(df)
        assert result[0].decision_timestamp.year == 2023

    def test_iso_string_timestamp_input(self) -> None:
        df = _make_orders_df(
            [
                {
                    "ts_init": "2023-11-14T22:13:20+00:00",
                    "instrument_id": "AAPL.NASDAQ",
                    "order_side": "BUY",
                    "quantity": "1",
                }
            ]
        )
        result = normalize_orders_df(df)
        assert result[0].decision_timestamp.year == 2023

    def test_unrecognized_side_raises(self) -> None:
        df = _make_orders_df(
            [
                {
                    "ts_init": 1_700_000_000_000_000_000,
                    "instrument_id": "AAPL.NASDAQ",
                    "order_side": "FLIP",
                    "quantity": "1",
                }
            ]
        )
        with pytest.raises(ValueError, match="unrecognized order side"):
            normalize_orders_df(df)

    def test_missing_required_column_raises_keyerror(self) -> None:
        """If none of the candidate timestamp columns are present,
        the resolver raises ``KeyError`` listing every name it
        tried — gives the caller actionable info on a Nautilus
        version mismatch."""
        df = _make_orders_df(
            [
                {
                    "instrument_id": "AAPL.NASDAQ",
                    "order_side": "BUY",
                    "quantity": "1",
                }
            ]
        )
        with pytest.raises(KeyError, match="ts_init"):
            normalize_orders_df(df)


# ---------------------------------------------------------------------------
# OrderIntent equality (sanity guard for the comparator's contract)
# ---------------------------------------------------------------------------


class TestOrderIntentEquality:
    def test_equal_intents_compare_equal(self) -> None:
        a = OrderIntent(
            decision_timestamp=datetime(2023, 1, 1, tzinfo=UTC),
            instrument_id="AAPL.NASDAQ",
            side="BUY",
            signed_qty=Decimal("1"),
        )
        b = OrderIntent(
            decision_timestamp=datetime(2023, 1, 1, tzinfo=UTC),
            instrument_id="AAPL.NASDAQ",
            side="BUY",
            signed_qty=Decimal("1"),
        )
        assert a == b
        assert hash(a) == hash(b)

    def test_different_qty_compares_unequal(self) -> None:
        a = OrderIntent(
            decision_timestamp=datetime(2023, 1, 1, tzinfo=UTC),
            instrument_id="AAPL.NASDAQ",
            side="BUY",
            signed_qty=Decimal("1"),
        )
        b = OrderIntent(
            decision_timestamp=datetime(2023, 1, 1, tzinfo=UTC),
            instrument_id="AAPL.NASDAQ",
            side="BUY",
            signed_qty=Decimal("2"),
        )
        assert a != b


# ---------------------------------------------------------------------------
# compare()
# ---------------------------------------------------------------------------


def _intent(ts: int, sym: str = "AAPL.NASDAQ", side: str = "BUY", qty: str = "1") -> OrderIntent:
    return OrderIntent(
        decision_timestamp=datetime.fromtimestamp(ts, tz=UTC),
        instrument_id=sym,
        side=side,
        signed_qty=Decimal(qty) if side == "BUY" else -Decimal(qty),
    )


class TestCompare:
    def test_identical_sequences_return_empty(self) -> None:
        a = [_intent(1), _intent(2), _intent(3)]
        b = [_intent(1), _intent(2), _intent(3)]
        assert compare(a, b) == []
        assert is_identical(a, b) is True

    def test_empty_sequences_match(self) -> None:
        assert compare([], []) == []
        assert is_identical([], []) is True

    def test_field_mismatch_at_index(self) -> None:
        """Different qty at index 1 → one ``FIELD_MISMATCH``
        record carrying both sides."""
        a = [_intent(1, qty="1"), _intent(2, qty="5"), _intent(3, qty="1")]
        b = [_intent(1, qty="1"), _intent(2, qty="6"), _intent(3, qty="1")]
        result = compare(a, b)
        assert len(result) == 1
        assert result[0].kind == DivergenceKind.FIELD_MISMATCH
        assert result[0].index == 1
        assert result[0].left == a[1]
        assert result[0].right == b[1]

    def test_extra_left_records_with_length_mismatch(self) -> None:
        """Left has more rows → ``EXTRA_LEFT`` per trailing row +
        a single ``LENGTH_MISMATCH`` summary."""
        a = [_intent(1), _intent(2), _intent(3)]
        b = [_intent(1)]
        result = compare(a, b)
        kinds = [d.kind for d in result]
        assert DivergenceKind.LENGTH_MISMATCH in kinds
        extras = [d for d in result if d.kind == DivergenceKind.EXTRA_LEFT]
        assert len(extras) == 2
        assert extras[0].index == 1
        assert extras[1].index == 2

    def test_extra_right_records_with_length_mismatch(self) -> None:
        a = [_intent(1)]
        b = [_intent(1), _intent(2), _intent(3)]
        result = compare(a, b)
        extras = [d for d in result if d.kind == DivergenceKind.EXTRA_RIGHT]
        assert len(extras) == 2
        assert any(d.kind == DivergenceKind.LENGTH_MISMATCH for d in result)

    def test_order_matters(self) -> None:
        """Same set of intents in different order is divergent —
        the contract is positional, not set-based, because a
        non-deterministic strategy that emits the same trades in
        a different order IS the bug we want to catch."""
        a = [_intent(1, sym="AAPL.NASDAQ"), _intent(2, sym="MSFT.NASDAQ")]
        b = [_intent(1, sym="MSFT.NASDAQ"), _intent(2, sym="AAPL.NASDAQ")]
        result = compare(a, b)
        assert len(result) == 2  # both index 0 and index 1 differ
        assert all(d.kind == DivergenceKind.FIELD_MISMATCH for d in result)

    def test_is_identical_returns_false_on_any_divergence(self) -> None:
        a = [_intent(1)]
        b = [_intent(1), _intent(2)]
        assert is_identical(a, b) is False
